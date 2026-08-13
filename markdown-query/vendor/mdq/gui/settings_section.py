"""Markdown-Query GUI settings section.

This module is the **single SoT** for the Markdown-Query GUI panel.
Both the HVE GUI (``hve.gui.settings_window``) and the standalone launcher
(``mdq.gui.standalone_window``) import the
``MdqIndexSection`` class from here.

UI structure (3 tabs):
    1. 基本 (Basic): language, chunking strategy, semantic options, target folders
    2. インデックス管理 (Index Management): stats, 「インデックス DB の管理」
       (差分更新 / 完全再ビルド / DB 削除 / 一括ビルド), Strategy 別統計, リアルタイム更新
    3. 統計情報 (Statistics): markdown-query Skill usage report (auto-regen)
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from . import index_service, settings_store
from ._widgets import LabeledField, TriStateCombo
from .index_service import SettingsBackend
from .threads import IndexRefreshThread, UsageReportThread

# Phase 3 additions
from .semantic_options import SemanticOptionsWidget
from .pageindex_options import PageIndexOptionsWidget
from .search_preview_panel import TestSearchPanel


def _all_strategies() -> tuple[str, ...]:
    """Return all chunking strategies known to mdq.

    SoT は ``mdq.strategies.ALL_STRATEGIES`` (Q4=K)。実装は
    ``settings_store.known_strategies()`` に委譲する (敵対的レビュー
    No.5 で DRY 違反を解消)。
    """
    return settings_store.known_strategies()


class MdqIndexSection(QWidget):
    """Markdown-Query management section (3 tabs).

    Attributes ``mdq_watch`` / ``mdq_watch_debounce_ms`` are exposed at the
    class instance level for compatibility with HVE's ``settings_apply``
    mapping.
    """

    # Auto-regen threshold: 24h.
    _AUTO_REGEN_THRESHOLD_SEC: int = 86400

    def __init__(
        self,
        *,
        repo_root: Path,
        parent: Optional[QWidget] = None,
        settings_backend: Optional[SettingsBackend] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._settings_store: SettingsBackend = settings_backend or settings_store
        self._refresh_thread: Optional[IndexRefreshThread] = None
        self._usage_thread: Optional[UsageReportThread] = None
        self._last_refresh_elapsed_ms: Optional[int] = None

        _saved = self._settings_store.load(repo_root).get("mdq", {})
        self._lang: str = str(_saved.get("tokenize_language", "ja-jp"))
        self._strategy: str = str(_saved.get("chunk_strategy", "heading"))
        try:
            self._overlap_paragraphs: int = int(
                _saved.get("overlap_paragraphs", 1)
            )
        except (TypeError, ValueError):
            self._overlap_paragraphs = 1
        self._target_folders: List[str] = settings_store.parse_target_folders(
            str(_saved.get("target_folders", ""))
        )
        # T17: 一括ビルド対象 Strategy 群。空は全選択扱い。
        self._build_strategies: List[str] = settings_store.parse_build_strategies(
            str(_saved.get("build_strategies", ""))
        )
        # T19: 一括ビルド用の状態。
        self._bulk_build_thread: Optional[IndexRefreshThread] = None
        self._bulk_build_queue: List[str] = []
        self._bulk_build_total: int = 0
        self._bulk_build_done: int = 0
        self._bulk_build_failed: List[tuple[str, str]] = []
        self._bulk_build_cancel_requested: bool = False

        self._lang_combo = QComboBox()
        self._lang_combo.addItem("ja-jp", "ja-jp")
        self._lang_combo.addItem("en-us", "en-us")
        idx_l = self._lang_combo.findData(self._lang)
        if idx_l >= 0:
            self._lang_combo.setCurrentIndex(idx_l)
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)

        self._strategy_combo = QComboBox()
        for s in _all_strategies():
            self._strategy_combo.addItem(s, s)
        idx_s = self._strategy_combo.findData(self._strategy)
        if idx_s >= 0:
            self._strategy_combo.setCurrentIndex(idx_s)
        self._strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)

        # Overlap paragraphs (heading_recursive 専用)
        self._overlap_spin = QSpinBox()
        self._overlap_spin.setRange(0, 5)
        self._overlap_spin.setValue(int(self._overlap_paragraphs))
        self._overlap_spin.setToolTip(self.tr(
            "heading_recursive 戦略専用: サブチャンク間で重ねる段落数。"
            "0 で overlap 無効。他の Strategy では無視されます。"
        ))
        self._overlap_spin.valueChanged.connect(self._on_overlap_changed)

        # --- Index stats ---
        self._stats_label = QLabel(self.tr("読み込み中..."))
        self._stats_label.setWordWrap(True)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        self._btn_incremental_refresh = QPushButton(self.tr("差分更新"))
        self._btn_incremental_refresh.setToolTip(self.tr(
            "変更された Markdown のみを再索引します (SHA-1 一致はスキップ)。"
            "全件をやり直したい場合は「完全再ビルド」を使用してください。"
        ))
        self._btn_incremental_refresh.clicked.connect(
            self._on_incremental_refresh_clicked
        )
        # Phase 3 (Q1=A 完全再ビルド + Q2=A DB 削除).
        self._btn_force_rebuild = QPushButton(self.tr("完全再ビルド"))
        self._btn_force_rebuild.setToolTip(self.tr(
            "SHA-1 一致でもスキップせず全 Markdown を再走査します。"
            "semantic_paragraph では埋め込みもやり直すため時間がかかります。"
        ))
        self._btn_force_rebuild.clicked.connect(self._on_force_rebuild_clicked)
        self._btn_delete_db = QPushButton(self.tr("DB を削除"))
        self._btn_delete_db.setToolTip(self.tr(
            "現在の (lang, strategy) の DB ファイルを削除します。"
            "削除後は「未作成」状態になり、再ビルドが必要です。"
        ))
        self._btn_delete_db.clicked.connect(self._on_delete_db_clicked)
        # Phase 3 (Q7=A ファイル単位進捗).
        self._refresh_progress = QProgressBar()
        self._refresh_progress.setVisible(False)
        self._refresh_progress.setRange(0, 100)
        # Phase 3: semantic_paragraph 専用設定 + 試し検索パネル。
        self._semantic_options_widget = SemanticOptionsWidget()
        self._semantic_options_widget.changed.connect(
            self._on_semantic_options_changed
        )
        _saved_mdq = _saved if isinstance(_saved, dict) else {}
        self._semantic_options_widget.load_from(_saved_mdq)
        self._semantic_options_widget.setVisible(
            self._strategy == "semantic_paragraph"
        )
        # pageindex 専用設定。
        self._pageindex_options_widget = PageIndexOptionsWidget()
        self._pageindex_options_widget.changed.connect(
            self._on_pageindex_options_changed
        )
        self._pageindex_options_widget.load_from(_saved_mdq)
        self._pageindex_options_widget.setVisible(
            self._strategy == "pageindex"
        )
        self._test_search_panel = TestSearchPanel(repo_root=repo_root)
        self._test_search_panel.set_context(
            lang=self._lang, strategy=self._strategy,
            fusion_alpha=self._resolve_fusion_alpha(),
        )

        # --- Usage stats ---
        self._usage_view = QTextBrowser()
        self._usage_view.setOpenExternalLinks(True)
        self._usage_view.setMinimumHeight(280)
        self._usage_result_label = QLabel("")
        self._usage_result_label.setWordWrap(True)
        self._btn_regen_usage = QPushButton(self.tr("利用統計レポートの再生成"))
        self._btn_regen_usage.clicked.connect(self._on_regen_usage_clicked)

        # ---- Tab 1: Basic ----
        tab_basic = QWidget()
        basic_layout = QVBoxLayout(tab_basic)
        basic_layout.setContentsMargins(8, 8, 8, 8)
        select_form = QFormLayout()
        select_form.addRow(self.tr("言語 (Tokenize)"), self._lang_combo)
        select_form.addRow(self.tr("Chunking Strategy"), self._strategy_combo)
        select_form.addRow(self.tr("Overlap (Paragraphs)"), self._overlap_spin)
        basic_layout.addLayout(select_form)
        # Phase 3: semantic_paragraph 専用設定（Strategy 切替で可視/不可視）。
        basic_layout.addWidget(self._semantic_options_widget)
        # pageindex 専用設定（Strategy 切替で可視/不可視）。
        basic_layout.addWidget(self._pageindex_options_widget)
        # 説明文は親ウィジェット幅に動的追従（word wrap のみ、固定幅不使用）。
        _desc_lang = QLabel(
            self.tr(
                "言語と Strategy ごとに別の DB インスタンス "
                "(.mdq/index-<lang>-<strategy>.sqlite) を使用します。"
                "初回は「差分更新」でビルドしてください。"
            )
        )
        _desc_lang.setWordWrap(True)
        _desc_lang.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        basic_layout.addWidget(_desc_lang)
        basic_layout.addSpacing(8)
        basic_layout.addWidget(QLabel(self.tr("<b>対象フォルダ</b>")))
        _desc_target = QLabel(self.tr(
            "Markdown-Query の索引対象と、Agent への Skill 利用強制対象を指定します。"
            "未設定の場合は mdq CLI の既定ルート (DEFAULT_ROOTS) を使用し、"
            "Agent への強制プロンプトは注入されません。"
            "パスはリポジトリルートからの相対パス (POSIX 形式) で保存されます。"
        ))
        _desc_target.setWordWrap(True)
        _desc_target.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        basic_layout.addWidget(_desc_target)
        self._target_folders_list = QListWidget()
        self._target_folders_list.setMinimumHeight(80)
        for f in self._target_folders:
            self._target_folders_list.addItem(QListWidgetItem(f))
        basic_layout.addWidget(self._target_folders_list)

        self._target_folders_input = QLineEdit()
        self._target_folders_input.setPlaceholderText(self.tr(
            "リポジトリ相対パスを入力 (例: docs/usecase) して Enter または [追加]"
        ))
        self._target_folders_input.returnPressed.connect(
            self._on_target_folder_add_from_input
        )
        basic_layout.addWidget(self._target_folders_input)

        btn_row = QHBoxLayout()
        self._btn_target_folder_pick = QPushButton(self.tr("フォルダを選択..."))
        self._btn_target_folder_pick.clicked.connect(
            self._on_target_folder_pick_clicked
        )
        self._btn_target_folder_add = QPushButton(self.tr("入力欄から追加"))
        self._btn_target_folder_add.clicked.connect(
            self._on_target_folder_add_from_input
        )
        self._btn_target_folder_remove = QPushButton(self.tr("選択行を削除"))
        self._btn_target_folder_remove.clicked.connect(
            self._on_target_folder_remove_clicked
        )
        btn_row.addWidget(self._btn_target_folder_pick)
        btn_row.addWidget(self._btn_target_folder_add)
        btn_row.addWidget(self._btn_target_folder_remove)
        btn_row.addStretch(1)
        basic_layout.addLayout(btn_row)

        self._target_folders_msg = QLabel("")
        self._target_folders_msg.setWordWrap(True)
        basic_layout.addWidget(self._target_folders_msg)

        # 一括ビルド関連 UI は index タブの「インデックス DB の管理」QGroupBox へ
        # 移されたため、basic タブでは widget 生成のみ行いレイアウトには追加しない。
        # widget 本体は「インデックス DB の管理」グループへ addWidget される。
        self._bulk_build_desc_label = QLabel(self.tr(
            "複数の Strategy のインデックスをまとめてビルドします。"
            "全 Strategy はクエリ時に自動選択 (query_router) で利用されるため、"
            "通常は全選択を推奨します。各 Strategy は別 DB ファイルに保存されます。"
        ))
        self._bulk_build_desc_label.setWordWrap(True)
        self._bulk_build_desc_label.setSizePolicy(
            QSizePolicy.Preferred, QSizePolicy.Minimum
        )
        self._build_strategies_list = QListWidget()
        self._build_strategies_list.setSelectionMode(
            QAbstractItemView.NoSelection
        )
        # 高さ制限は設けず、全 Strategy をスクロールなしで表示する。
        _strategy_names = list(_all_strategies())
        _selected_set = set(self._build_strategies)
        for s in _strategy_names:
            item = QListWidgetItem(s)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if (not _selected_set) or (s in _selected_set)
                else Qt.CheckState.Unchecked
            )
            self._build_strategies_list.addItem(item)
        self._build_strategies_list.itemChanged.connect(
            self._on_build_strategies_changed
        )
        # 全件をスクロールなしで収めるため、必要高さを計算して固定する。
        # sizeHintForRow(0) は widget 未 realize 時に 0 以下を返すことがあるため、
        # fontMetrics + padding を下限フォールバックとして使う。
        _list = self._build_strategies_list
        _hinted = _list.sizeHintForRow(0) if _list.count() > 0 else 0
        _row_h_fallback = self.fontMetrics().height() + 6
        _row_h = max(_hinted, _row_h_fallback)
        _frame = 2 * _list.frameWidth()
        _fixed_h = _row_h * _list.count() + _frame + 8
        _list.setFixedHeight(_fixed_h)
        _list.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._btn_bulk_build = QPushButton(
            self.tr("選択 Strategy を一括ビルド")
        )
        self._btn_bulk_build.clicked.connect(self._on_bulk_build_clicked)
        self._btn_bulk_cancel = QPushButton(
            self.tr("ビルドを停止（実行中 Strategy 完了後）")
        )
        self._btn_bulk_cancel.setEnabled(False)
        self._btn_bulk_cancel.clicked.connect(self._on_bulk_build_cancel_clicked)
        self._bulk_build_msg = QLabel("")
        self._bulk_build_msg.setWordWrap(True)

        basic_layout.addStretch(1)

        # ---- Tab 2: Index management ----
        tab_index = QWidget()
        index_layout = QVBoxLayout(tab_index)
        index_layout.setContentsMargins(8, 8, 8, 8)
        index_layout.addWidget(QLabel(self.tr("<b>インデックスの統計情報</b>")))
        index_layout.addWidget(QLabel(
            self.tr("Markdown 索引の現在の規模・鮮度を表示します。")
        ))
        _stats_row = QHBoxLayout()
        _stats_row.setContentsMargins(0, 0, 0, 0)
        _stats_row.addWidget(self._stats_label)
        _stats_row.addStretch(1)
        index_layout.addLayout(_stats_row)
        index_layout.addSpacing(8)

        # ---- インデックス DB の管理 (QGroupBox) -----------------------
        # 差分更新 / 完全再ビルド / DB 削除 / 一括ビルド を統合して表示。
        _db_mgmt_group = QGroupBox(self.tr("インデックス DB の管理"))
        _db_mgmt_layout = QVBoxLayout(_db_mgmt_group)
        _db_mgmt_layout.setContentsMargins(8, 8, 8, 8)

        self._btn_incremental_refresh.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed
        )
        # 隣接ボタンも同 SizePolicy で並べる（一貫性）。
        self._btn_force_rebuild.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed
        )
        self._btn_delete_db.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed
        )
        _incremental_refresh_row = QHBoxLayout()
        _incremental_refresh_row.setContentsMargins(0, 0, 0, 0)
        _incremental_refresh_row.addWidget(self._btn_incremental_refresh)
        _incremental_refresh_row.addWidget(self._btn_force_rebuild)
        _incremental_refresh_row.addWidget(self._btn_delete_db)
        _incremental_refresh_row.addStretch(1)
        _db_mgmt_layout.addLayout(_incremental_refresh_row)
        _db_mgmt_layout.addWidget(self._refresh_progress)
        _db_mgmt_layout.addWidget(self._result_label)

        # --- 一括ビルド対象 Strategy (T18/T19/T20 から移設) ---
        _db_mgmt_layout.addSpacing(8)
        _db_mgmt_layout.addWidget(
            QLabel(self.tr("<b>一括ビルド対象 Strategy</b>"))
        )
        _db_mgmt_layout.addWidget(self._bulk_build_desc_label)
        _db_mgmt_layout.addWidget(self._build_strategies_list)
        _bulk_btn_row = QHBoxLayout()
        _bulk_btn_row.addWidget(self._btn_bulk_build)
        _bulk_btn_row.addWidget(self._btn_bulk_cancel)
        _bulk_btn_row.addStretch(1)
        _db_mgmt_layout.addLayout(_bulk_btn_row)
        _db_mgmt_layout.addWidget(self._bulk_build_msg)

        index_layout.addWidget(_db_mgmt_group)

        # ---- Strategy 別統計表 (T15) -----------------------------------
        index_layout.addSpacing(12)
        index_layout.addWidget(QLabel(
            self.tr("<b>Strategy 別インデックス統計</b>")
        ))
        index_layout.addWidget(QLabel(self.tr(
            "全 Chunking Strategy について DB 存在・ファイル数・チャンク数・"
            "最終更新時刻を表示します。未生成の Strategy は files=0 / "
            "chunks=0 で表示されます。"
        )))
        self._strategy_stats_table = QTableWidget()
        self._strategy_stats_table.setColumnCount(5)
        self._strategy_stats_table.setHorizontalHeaderLabels([
            self.tr("Strategy"),
            self.tr("DB 存在"),
            self.tr("Files"),
            self.tr("Chunks"),
            self.tr("最終更新"),
        ])
        self._strategy_stats_table.verticalHeader().setVisible(False)
        self._strategy_stats_table.setEditTriggers(
            QAbstractItemView.NoEditTriggers
        )
        self._strategy_stats_table.setSelectionMode(
            QAbstractItemView.NoSelection
        )
        _hdr = self._strategy_stats_table.horizontalHeader()
        _hdr.setSectionResizeMode(QHeaderView.ResizeToContents)
        _hdr.setStretchLastSection(True)
        # 行高はコンパクトに。Strategy 数 + ヘッダ分の高さを最低保証。
        self._strategy_stats_table.setMinimumHeight(
            24 * (max(len(_all_strategies()), 3) + 1) + 8
        )
        index_layout.addWidget(self._strategy_stats_table)

        index_layout.addSpacing(16)
        index_layout.addWidget(QLabel(self.tr("<b>リアルタイム更新</b>")))
        self.mdq_watch = TriStateCombo()
        index_layout.addWidget(LabeledField(
            title=self.tr("mdq リアルタイム更新"),
            description=self.tr(
                "Markdown ファイルの追加/更新/削除を OS イベントで検知し "
                ".mdq/index.sqlite を逐次更新。"
                "既定: ON（watchdog 未導入時は自動で無効化、警告ログのみ）。"
            ),
            input_widget=self.mdq_watch,
        ))
        self.mdq_watch_debounce_ms = QSpinBox()
        self.mdq_watch_debounce_ms.setRange(0, 60000)
        self.mdq_watch_debounce_ms.setValue(0)
        self.mdq_watch_debounce_ms.setSpecialValueText(
            self.tr("（既定 500ms を使用）")
        )
        index_layout.addWidget(LabeledField(
            title=self.tr("mdq watcher デバウンス間隔 (ms)"),
            description=self.tr("0 のとき既定 500 ms を使用。"),
            input_widget=self.mdq_watch_debounce_ms,
        ))
        # Phase 3: 試し検索パネル（Q4=B 既定折りたたみ）。
        index_layout.addSpacing(16)
        index_layout.addWidget(self._test_search_panel)
        index_layout.addStretch(1)

        # ---- Tab 3: Statistics ----
        tab_stats = QWidget()
        stats_layout = QVBoxLayout(tab_stats)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.addWidget(QLabel(
            self.tr("<b>markdown-query Skill 利用統計情報</b>")
        ))
        stats_layout.addWidget(QLabel(self.tr(
            "Skill が想定通り使われているかを 15 指標で測定します。"
            "各指標の定義は users-guide/skills-markdown-query.md を参照。"
            "起動時に最新レポートが 24 時間以上前であれば自動再生成します。"
        )))
        _usage_row = QHBoxLayout()
        _usage_row.setContentsMargins(0, 0, 0, 0)
        _usage_row.addWidget(self._usage_view, 1)
        _usage_row.addStretch(1)
        stats_layout.addLayout(_usage_row, 1)
        stats_layout.addSpacing(4)
        self._btn_regen_usage.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        _regen_usage_row = QHBoxLayout()
        _regen_usage_row.setContentsMargins(0, 0, 0, 0)
        _regen_usage_row.addWidget(self._btn_regen_usage)
        _regen_usage_row.addStretch(1)
        stats_layout.addLayout(_regen_usage_row)
        stats_layout.addWidget(self._usage_result_label)

        # ---- Tabs ----
        self._tabs = QTabWidget(self)
        self._tabs.addTab(tab_basic, self.tr("基本"))
        self._tabs.addTab(tab_index, self.tr("インデックス管理"))
        self._tabs.addTab(tab_stats, self.tr("統計情報"))

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(self._tabs)

        self._load_stats()
        self._load_usage_stats(auto_regen_if_stale=True)

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        half = max(200, self.width() // 2)
        if hasattr(self, "_stats_label"):
            self._stats_label.setMaximumWidth(half)
        if hasattr(self, "_usage_view"):
            self._usage_view.setMaximumWidth(half)

    # ----------------------------------------------------------
    # target_folders ops
    # ----------------------------------------------------------
    def _add_target_folder(self, raw_path: str) -> None:
        norm = settings_store._normalize_target_folder(raw_path)
        if norm is None:
            self._target_folders_msg.setText(self.tr("空のパスは追加できません。"))
            return
        candidate = Path(norm)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(self._repo_root.resolve())
                norm = rel.as_posix() or "."
                if norm == ".":
                    self._target_folders_msg.setText(self.tr(
                        "リポジトリルートそのものは対象に追加できません。"
                    ))
                    return
            except ValueError:
                self._target_folders_msg.setText(
                    self.tr("リポジトリ外のフォルダは追加できません: ") + raw_path
                )
                return
        if norm in self._target_folders:
            self._target_folders_msg.setText(self.tr("既に追加済みです: ") + norm)
            return
        self._target_folders.append(norm)
        self._target_folders_list.addItem(QListWidgetItem(norm))
        self._persist_settings()
        self._target_folders_msg.setText(self.tr("追加しました: ") + norm)

    def _on_target_folder_pick_clicked(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            self.tr("対象フォルダを選択 (リポジトリ配下)"),
            str(self._repo_root),
        )
        if not path:
            return
        self._add_target_folder(path)

    def _on_target_folder_add_from_input(self) -> None:
        raw = self._target_folders_input.text().strip()
        if not raw:
            return
        self._add_target_folder(raw)
        self._target_folders_input.clear()

    def _on_target_folder_remove_clicked(self) -> None:
        selected = self._target_folders_list.selectedItems()
        if not selected:
            self._target_folders_msg.setText(self.tr("削除対象を選択してください。"))
            return
        removed: list[str] = []
        for item in selected:
            text = item.text()
            row = self._target_folders_list.row(item)
            self._target_folders_list.takeItem(row)
            if text in self._target_folders:
                self._target_folders.remove(text)
                removed.append(text)
        self._persist_settings()
        if removed:
            self._target_folders_msg.setText(
                self.tr("削除しました: ") + ", ".join(removed)
            )

    # ----------------------------------------------------------
    # Language / Strategy switch
    # ----------------------------------------------------------
    def _on_lang_changed(self) -> None:
        self._lang = str(self._lang_combo.currentData() or "ja-jp")
        self._persist_settings()
        self._load_stats()
        self._load_usage_stats(auto_regen_if_stale=False)
        self._usage_result_label.setText(self.tr(
            "言語を切り替えました。現在の選択を反映したレポートは「利用統計レポートの再生成」を"
            "押してください。インデックス未生成の場合は「差分更新」も実行してください。"
        ))

    def _on_strategy_changed(self) -> None:
        self._strategy = str(self._strategy_combo.currentData() or "heading")
        self._persist_settings()
        self._load_stats()
        self._load_usage_stats(auto_regen_if_stale=False)
        # Phase 3: semantic_paragraph 専用 widget の可視性を更新。
        try:
            self._semantic_options_widget.setVisible(
                self._strategy == "semantic_paragraph"
            )
        except AttributeError:
            pass  # widget 未構築（移行期防御）
        # pageindex 専用 widget の可視性を更新。
        try:
            self._pageindex_options_widget.setVisible(
                self._strategy == "pageindex"
            )
        except AttributeError:
            pass
        try:
            self._test_search_panel.set_context(
                lang=self._lang, strategy=self._strategy,
                fusion_alpha=self._resolve_fusion_alpha(),
            )
        except AttributeError:
            pass
        self._usage_result_label.setText(self.tr(
            "Strategy を切り替えました。現在の選択を反映したレポートは「利用統計レポートの再生成」を"
            "押してください。インデックス未生成の場合は「差分更新」も実行してください。"
        ))

    def _on_overlap_changed(self, value: int) -> None:
        self._overlap_paragraphs = int(value)
        self._persist_settings()

    def _on_semantic_options_changed(self) -> None:
        """SemanticOptionsWidget の変更を [mdq] へ永続化する。"""
        self._persist_settings()
        # Test search panel に fusion_alpha 変更を反映。
        try:
            self._test_search_panel.set_context(
                lang=self._lang, strategy=self._strategy,
                fusion_alpha=self._resolve_fusion_alpha(),
            )
        except AttributeError:
            pass

    def _on_pageindex_options_changed(self) -> None:
        """PageIndexOptionsWidget の変更を [mdq] へ永続化する。"""
        self._persist_settings()

    def _resolve_fusion_alpha(self) -> float | None:
        """Q9=A: late_chunking ON + semantic_paragraph 時のみ alpha を返す。"""
        if self._strategy != "semantic_paragraph":
            return None
        try:
            opts = self._semantic_options_widget.to_settings_dict()
        except AttributeError:
            return None
        if not opts.get("semantic_late_chunking"):
            return None
        try:
            return float(opts.get("semantic_fusion_alpha", 0.5))
        except (TypeError, ValueError):
            return None

    def _on_force_rebuild_clicked(self) -> None:
        """Q1=A 完全再ビルド。確認ダイアログ後に rebuild=True で実行。"""
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        if (
            self._bulk_build_thread is not None
            and self._bulk_build_thread.isRunning()
        ):
            self._result_label.setText(self.tr(
                "「一括ビルド」が実行中です。完了後に再試行してください。"
            ))
            return
        ret = QMessageBox.question(
            self,
            self.tr("完全再ビルドの確認"),
            self.tr(
                "現在の (言語, Strategy) のインデックスを完全に再ビルドします。\n"
                "既存の SHA-1 一致もスキップせず、すべての Markdown を再走査します。\n"
                "semantic_paragraph では埋め込みもやり直すため時間がかかります。\n\n"
                "続行しますか？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        # semantic_paragraph 用 runtime options を組み立てる。
        sem_opts = None
        if self._strategy == "semantic_paragraph":
            try:
                sem_opts = settings_store.get_semantic_runtime_config(
                    self._repo_root,
                    settings=self._settings_store.load(self._repo_root),
                )
            except Exception:  # noqa: BLE001 -- defensive
                sem_opts = None
        # pageindex 用 runtime options を組み立てる。
        pi_opts = None
        if self._strategy == "pageindex":
            try:
                pi_opts = self._pageindex_options_widget.to_runtime_kwargs()
            except AttributeError:
                pi_opts = None
        self._start_refresh_thread(
            force=True, semantic_options=sem_opts, pageindex_options=pi_opts,
        )

    def _on_delete_db_clicked(self) -> None:
        """Q2=A 個別 DB 削除。二重確認後に unlink。"""
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            self._result_label.setText(self.tr(
                "インデックス更新中です。完了後に再試行してください。"
            ))
            return
        ret = QMessageBox.warning(
            self,
            self.tr("DB 削除の確認"),
            self.tr(
                "現在の (言語, Strategy) の DB ファイルを削除します。\n"
                "削除後は「未作成」状態になり、再ビルドが必要です。\n\n"
                "本当に削除しますか？"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            r = index_service.delete_index_db(
                self._repo_root, lang=self._lang, strategy=self._strategy,
            )
        except OSError as e:
            self._result_label.setText(self.tr(
                "DB 削除に失敗: 別プロセス（mdq watcher 等）が開いている可能性があります。"
            ) + f" ({e})")
            return
        if r.get("deleted"):
            self._result_label.setText(self.tr(
                "DB を削除しました: "
            ) + str(r.get("db_path", "")))
        else:
            self._result_label.setText(self.tr(
                "DB は存在しませんでした（既に削除済み）。"
            ))
        self._load_stats()

    def _start_refresh_thread(
        self, *, force: bool = False, semantic_options: dict | None = None,
        pageindex_options: dict | None = None,
    ) -> None:
        """共通: refresh thread の起動。差分更新 / 完全再ビルド両方が利用。"""
        self._btn_incremental_refresh.setEnabled(False)
        self._btn_force_rebuild.setEnabled(False)
        self._btn_delete_db.setEnabled(False)
        self._refresh_progress.setValue(0)
        self._refresh_progress.setVisible(True)
        self._result_label.setText(
            self.tr("インデックスを完全再ビルドしています...")
            if force else
            self.tr("インデックスを更新しています...")
        )
        self._refresh_thread = IndexRefreshThread(
            repo_root=self._repo_root,
            lang=self._lang,
            strategy=self._strategy,
            overlap_paragraphs=int(self._overlap_paragraphs),
            force=force,
            semantic_options=semantic_options,
            pageindex_options=pageindex_options,
            settings_backend=self._settings_store,
            parent=self,
        )
        self._refresh_thread.succeeded.connect(self._on_refresh_succeeded)
        self._refresh_thread.failed.connect(self._on_refresh_failed)
        self._refresh_thread.finished.connect(self._on_refresh_finished)
        self._refresh_thread.progressed.connect(self._on_refresh_progressed)
        self._refresh_thread.start()

    def _on_refresh_progressed(self, rel: str, cur: int, total: int) -> None:
        if total > 0:
            self._refresh_progress.setRange(0, total)
            self._refresh_progress.setValue(cur)
        self._result_label.setText(
            self.tr("索引中: ") + f"{cur}/{total} — {rel}"
        )

    def _persist_settings(self) -> None:
        try:
            cur = self._settings_store.load(self._repo_root)
            mdq = dict(cur.get("mdq", {}))
            mdq["tokenize_language"] = self._lang
            mdq["chunk_strategy"] = self._strategy
            mdq["overlap_paragraphs"] = int(self._overlap_paragraphs)
            mdq["target_folders"] = settings_store.serialize_target_folders(
                self._target_folders
            )
            mdq["build_strategies"] = settings_store.serialize_build_strategies(
                self._build_strategies
            )
            # Phase 3: semantic_paragraph 専用 widget の値を merge。
            try:
                mdq.update(self._semantic_options_widget.to_settings_dict())
            except AttributeError:
                pass  # 移行期防御
            # pageindex 専用 widget の値を merge。
            try:
                mdq.update(self._pageindex_options_widget.to_settings_dict())
            except AttributeError:
                pass  # 移行期防御
            cur["mdq"] = mdq
            self._settings_store.save(self._repo_root, cur)
        except Exception as exc:  # pragma: no cover - defensive
            self._usage_result_label.setText(
                self.tr("設定保存に失敗しました: ") + str(exc)
            )

    # ----------------------------------------------------------
    # Index stats
    # ----------------------------------------------------------
    def _format_stats(self, stats: dict) -> str:
        root_lines: List[str] = []
        for item in stats.get("root_stats", []):
            root_lines.append(
                f"- {item.get('root', '-')}: files={item.get('files', 0)}, "
                f"chunks={item.get('chunks', 0)}"
            )
        roots_block = "\n".join(root_lines) if root_lines else "- なし"
        if self._last_refresh_elapsed_ms is None:
            elapsed = "未実行（この起動）"
        else:
            elapsed = f"{self._last_refresh_elapsed_ms} ms"
        return (
            f"DB: {stats.get('db_path', '-')}\n"
            f"最終更新: {stats.get('db_mtime', '-')}\n"
            f"Schema Version: {stats.get('schema_version', '-')}\n"
            f"FTS5: {'有効' if stats.get('fts5_enabled') else '無効'}\n"
            f"ファイル数: {stats.get('files', 0)}\n"
            f"チャンク数: {stats.get('chunks', 0)}\n"
            f"前回差分更新時間: {elapsed}\n"
            "ルート別件数:\n"
            f"{roots_block}"
        )

    def _format_summary(self, summary: dict) -> str:
        return (
            "差分更新完了: "
            f"files_indexed={summary.get('files_indexed', 0)}, "
            f"files_skipped={summary.get('files_skipped', 0)}, "
            f"chunks_written={summary.get('chunks_written', 0)}, "
            f"pruned_files={summary.get('pruned_files', 0)}, "
            f"pruned_chunks={summary.get('pruned_chunks', 0)}, "
            f"elapsed_ms={summary.get('elapsed_ms', 0)}"
        )

    def _load_stats(self) -> None:
        try:
            stats = index_service.get_index_stats(
                self._repo_root,
                lang=self._lang,
                strategy=self._strategy,
                settings_backend=self._settings_store,
            )
            self._stats_label.setText(self._format_stats(stats))
        except Exception as e:  # pragma: no cover - defensive
            self._stats_label.setText(f"統計情報の取得に失敗しました: {e}")
        # T15: Strategy 別統計表も同タイミングで更新する。
        self._load_strategy_stats_table()

    def _load_strategy_stats_table(self) -> None:
        """T15: 全 Strategy の統計を取得して ``_strategy_stats_table`` に表示する。"""
        if not hasattr(self, "_strategy_stats_table"):
            return  # __init__ 途中の早期呼び出しガード
        try:
            all_stats = index_service.get_index_stats_all_strategies(
                self._repo_root,
                lang=self._lang,
                settings_backend=self._settings_store,
            )
        except Exception as e:  # pragma: no cover - defensive
            self._strategy_stats_table.setRowCount(0)
            self._strategy_stats_table.setRowCount(1)
            err_item = QTableWidgetItem(f"統計取得失敗: {e}")
            self._strategy_stats_table.setItem(0, 0, err_item)
            self._strategy_stats_table.setSpan(
                0, 0, 1, self._strategy_stats_table.columnCount()
            )
            return
        strategies = list(_all_strategies())
        self._strategy_stats_table.setRowCount(len(strategies))
        for row, strategy in enumerate(strategies):
            st = all_stats.get(strategy, {})
            db_exists = bool(st.get("db_exists", False))
            files = int(st.get("files", 0))
            chunks = int(st.get("chunks", 0))
            # No.18: error フィールドがあれば最終更新列にエラー要約を表示
            err = st.get("error")
            if err:
                mtime = self.tr("エラー: ") + (
                    str(err)[:40] + "…" if len(str(err)) > 40 else str(err)
                )
            else:
                mtime = str(st.get("db_mtime", "未作成"))
            values = [
                strategy,
                self.tr("有り") if db_exists else self.tr("無し"),
                str(files),
                str(chunks),
                mtime,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col >= 2:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self._strategy_stats_table.setItem(row, col, item)

    # ----------------------------------------------------------
    # Bulk build (T18 / T19 / T20)
    # ----------------------------------------------------------
    def _on_build_strategies_changed(self, item: QListWidgetItem) -> None:
        """ビルド対象 Strategy のチェック状態が変わったときの保存処理。"""
        selected: List[str] = []
        for i in range(self._build_strategies_list.count()):
            it = self._build_strategies_list.item(i)
            if it is None:
                continue
            if it.checkState() == Qt.CheckState.Checked:
                selected.append(it.text())
        # 全て解除された場合は「全選択扱い」(serialize 側で空文字列保存) と
        # 区別するため、内部状態は実選択をそのまま保持する。
        self._build_strategies = selected
        self._persist_settings()

    def _bulk_build_selected_strategies(self) -> List[str]:
        """現在チェックされている Strategy のリスト。

        in-memory `self._build_strategies` をそのまま返す（空は空のまま）。
        敵対的レビュー No.2 修正: 「空選択 = 全選択扱い」とすると、ユーザーが
        全チェックを外して「一括ビルド」を押したときに警告メッセージが
        到達不能 (dead code) になり、意図しないビルドが走るため。
        永続化形式 (``serialize_build_strategies`` の空文字列規約) とは
        別レイヤとして扱う。
        """
        return list(self._build_strategies)

    def _on_bulk_build_clicked(self) -> None:
        """T19: 選択 Strategy を直列に一括ビルド開始。"""
        if (
            self._bulk_build_thread is not None
            and self._bulk_build_thread.isRunning()
        ):
            return
        # No.3: 単一 Strategy の差分更新と相互排他
        if (
            self._refresh_thread is not None
            and self._refresh_thread.isRunning()
        ):
            self._bulk_build_msg.setText(self.tr(
                "「差分更新」が実行中です。完了後に再試行してください。"
            ))
            return
        targets = self._bulk_build_selected_strategies()
        if not targets:
            self._bulk_build_msg.setText(self.tr(
                "ビルド対象 Strategy が 1 つも選択されていません。"
            ))
            return
        self._bulk_build_queue = list(targets)
        self._bulk_build_total = len(targets)
        self._bulk_build_done = 0
        self._bulk_build_failed = []
        self._bulk_build_cancel_requested = False
        self._btn_bulk_build.setEnabled(False)
        self._btn_bulk_cancel.setEnabled(True)
        # 差分更新ボタンも実行中は無効化 (相互排他)
        self._btn_incremental_refresh.setEnabled(False)
        self._bulk_build_msg.setText(self.tr(
            "一括ビルドを開始しました: 0/{total} 完了"
        ).format(total=self._bulk_build_total))
        self._run_next_bulk_build()

    def _run_next_bulk_build(self) -> None:
        """キューから次の Strategy を取り出して 1 ジョブ実行。"""
        if self._bulk_build_cancel_requested or not self._bulk_build_queue:
            self._finalize_bulk_build()
            return
        next_strategy = self._bulk_build_queue.pop(0)
        self._bulk_build_msg.setText(self.tr(
            "{done}/{total} 完了 — '{strategy}' をビルド中..."
        ).format(
            done=self._bulk_build_done,
            total=self._bulk_build_total,
            strategy=next_strategy,
        ))
        self._bulk_build_thread = IndexRefreshThread(
            repo_root=self._repo_root,
            lang=self._lang,
            strategy=next_strategy,
            overlap_paragraphs=int(self._overlap_paragraphs),
            settings_backend=self._settings_store,
            parent=self,
        )
        # 失敗時もキューを進めるため、succeeded/failed の両方を捕捉。
        # No.4: succeeded は payload のみ受け取り (strategy は内部状態で追跡)
        self._bulk_build_thread.succeeded.connect(
            lambda _summary: self._on_bulk_build_step_ok()
        )
        self._bulk_build_thread.failed.connect(
            lambda msg, strategy=next_strategy: self._on_bulk_build_step_fail(
                strategy, msg
            )
        )
        self._bulk_build_thread.finished.connect(self._on_bulk_build_step_done)
        self._bulk_build_thread.start()

    def _on_bulk_build_step_ok(self) -> None:
        self._bulk_build_done += 1

    def _on_bulk_build_step_fail(self, strategy: str, msg: str) -> None:
        self._bulk_build_done += 1
        self._bulk_build_failed.append((strategy, msg))

    def _on_bulk_build_step_done(self) -> None:
        """1 つの Strategy ビルドが完了 → 次へ。"""
        self._bulk_build_thread = None
        # 統計表は逐次更新（進捗のフィードバック強化）。
        self._load_strategy_stats_table()
        self._run_next_bulk_build()

    def _on_bulk_build_cancel_clicked(self) -> None:
        """T20: キャンセル要求。実行中ジョブの完了後にキューを空にする。"""
        self._bulk_build_cancel_requested = True
        self._bulk_build_queue = []
        self._bulk_build_msg.setText(self.tr(
            "キャンセル要求を受理しました。実行中の Strategy 完了後に停止します。"
        ))
        self._btn_bulk_cancel.setEnabled(False)

    def _finalize_bulk_build(self) -> None:
        """一括ビルドの最終結果を表示し、UI 状態を戻す。"""
        self._btn_bulk_build.setEnabled(True)
        self._btn_bulk_cancel.setEnabled(False)
        # No.3: 相互排他解除
        self._btn_incremental_refresh.setEnabled(True)
        cancelled = self._bulk_build_cancel_requested
        failed = self._bulk_build_failed
        done = self._bulk_build_done
        total = self._bulk_build_total
        if cancelled and done < total:
            self._bulk_build_msg.setText(self.tr(
                "キャンセル完了: {done}/{total} 完了。失敗 {nfail} 件。"
            ).format(done=done, total=total, nfail=len(failed)))
        elif failed:
            # No.13: 失敗メッセージは 60 字で切り詰めて UI 崩壊を防ぐ。
            failed_summary = ", ".join(
                f"{s}: {(m[:60] + '…') if len(m) > 60 else m}"
                for s, m in failed[:3]
            )
            more = "" if len(failed) <= 3 else f" 他 {len(failed) - 3} 件"
            self._bulk_build_msg.setText(self.tr(
                "完了 (一部失敗): {done}/{total} 完了。失敗 {nfail} 件 — {summary}{more}"
            ).format(
                done=done,
                total=total,
                nfail=len(failed),
                summary=failed_summary,
                more=more,
            ))
        else:
            self._bulk_build_msg.setText(self.tr(
                "完了: {done}/{total} 全 Strategy のビルドが成功しました。"
            ).format(done=done, total=total))
        self._load_stats()
        # 状態リセット
        self._bulk_build_thread = None
        self._bulk_build_queue = []
        self._bulk_build_failed = []
        self._bulk_build_cancel_requested = False

    def _on_incremental_refresh_clicked(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.isRunning():
            return
        # No.3: 一括ビルドと相互排他
        if (
            self._bulk_build_thread is not None
            and self._bulk_build_thread.isRunning()
        ):
            self._result_label.setText(self.tr(
                "「一括ビルド」が実行中です。完了後に再試行してください。"
            ))
            return
        # Q5=A: semantic_paragraph 選択 + extras 未インストール時はガード。
        if (
            self._strategy == "semantic_paragraph"
            and not self._semantic_options_widget.semantic_ok
        ):
            self._result_label.setText(self.tr(
                "[semantic] extra が未インストールです。"
                "ビルドを開始すると heading_recursive へフォールバックします。"
            ))
        sem_opts = None
        if self._strategy == "semantic_paragraph":
            try:
                sem_opts = settings_store.get_semantic_runtime_config(
                    self._repo_root,
                    settings=self._settings_store.load(self._repo_root),
                )
            except Exception:  # noqa: BLE001 -- defensive
                sem_opts = None
        pi_opts = None
        if self._strategy == "pageindex":
            try:
                pi_opts = self._pageindex_options_widget.to_runtime_kwargs()
            except AttributeError:
                pi_opts = None
        self._start_refresh_thread(
            force=False, semantic_options=sem_opts, pageindex_options=pi_opts,
        )

    def _on_refresh_succeeded(self, summary: dict) -> None:
        self._last_refresh_elapsed_ms = int(summary.get("elapsed_ms", 0))
        self._result_label.setText(self._format_summary(summary))
        self._load_stats()

    def _on_refresh_failed(self, message: str) -> None:
        self._result_label.setText(f"更新失敗: {message}")

    def _on_refresh_finished(self) -> None:
        self._btn_incremental_refresh.setEnabled(True)
        self._btn_force_rebuild.setEnabled(True)
        self._btn_delete_db.setEnabled(True)
        self._refresh_progress.setVisible(False)
        self._refresh_thread = None

    # ----------------------------------------------------------
    # Usage stats
    # ----------------------------------------------------------
    def _latest_report_path(self) -> Path:
        from mdq import usage_report

        return usage_report.default_output_dir(self._repo_root) / "latest.md"

    def _is_report_stale(self) -> bool:
        p = self._latest_report_path()
        if not p.exists():
            return True
        import time as _t
        age = _t.time() - p.stat().st_mtime
        return age > self._AUTO_REGEN_THRESHOLD_SEC

    def _load_usage_stats(self, *, auto_regen_if_stale: bool = False) -> None:
        p = self._latest_report_path()
        if not p.exists():
            self._usage_view.setMarkdown(self.tr(
                "（レポート未生成。下のボタンで生成してください。"
                "起動時自動生成は数秒〜十秒程度かかります。）"
            ))
        else:
            try:
                self._usage_view.setMarkdown(p.read_text(encoding="utf-8"))
            except OSError as e:  # pragma: no cover - defensive
                self._usage_view.setPlainText(f"レポート読み込み失敗: {e}")
        if auto_regen_if_stale and self._is_report_stale():
            self._start_regen()

    def _start_regen(self) -> None:
        if self._usage_thread is not None and self._usage_thread.isRunning():
            return
        self._btn_regen_usage.setEnabled(False)
        self._usage_result_label.setText(
            self.tr("利用統計レポートを生成しています...")
        )
        self._usage_thread = UsageReportThread(
            repo_root=self._repo_root,
            lang=self._lang,
            strategy=self._strategy,
            parent=self,
        )
        self._usage_thread.succeeded.connect(self._on_usage_succeeded)
        self._usage_thread.failed.connect(self._on_usage_failed)
        self._usage_thread.finished.connect(self._on_usage_finished)
        self._usage_thread.start()

    def _on_regen_usage_clicked(self) -> None:
        self._start_regen()

    def _on_usage_succeeded(self, paths: dict) -> None:
        self._usage_result_label.setText(f"生成完了: {paths.get('md', '-')}")
        self._load_usage_stats(auto_regen_if_stale=False)

    def _on_usage_failed(self, message: str) -> None:
        self._usage_result_label.setText(f"レポート生成失敗: {message}")

    def _on_usage_finished(self) -> None:
        self._btn_regen_usage.setEnabled(True)
        self._usage_thread = None
