"""Shared three-tab management panel for Code Query (FR-GUI-04)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
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

from cq import golden_eval
from cq import search as cq_search
from cq import store as cq_store
from cq import watcher as cq_watcher

from . import index_service, settings_store as standalone_settings_store
from .threads import CqBenchmarkThread, CqIndexBuildThread, CqSearchPreviewThread
from .widgets import LabeledField, TriStateCombo


class SettingsBackend(Protocol):
    def load(self, repo_root: Path) -> dict[str, dict[str, Any]]: ...
    def save(self, repo_root: Path, settings: dict[str, dict[str, Any]]) -> None: ...
    def parse_semicolon_list(self, raw: str) -> list[str]: ...
    def serialize_semicolon_list(self, values: list[str]) -> str: ...


class CqIndexSection(QWidget):
    """Code Query management panel shared by standalone and HVE hosts."""

    def __init__(
        self,
        *,
        repo_root: Path,
        parent: Optional[QWidget] = None,
        settings_backend: Optional[SettingsBackend] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = Path(repo_root).resolve()
        self._settings_store: SettingsBackend = (
            settings_backend or standalone_settings_store
        )
        self._build_thread: Optional[CqIndexBuildThread] = None
        self._building_profile = ""
        self._preview_thread: Optional[CqSearchPreviewThread] = None
        self._benchmark_thread: Optional[CqBenchmarkThread] = None
        self._bulk_queue: List[str] = []
        self._bulk_failed: List[tuple[str, str]] = []
        self._bulk_total = 0
        self._bulk_cancel = False

        listing = index_service.list_profiles(self._repo_root)
        self._profiles: dict[str, Any] = listing["profiles"]
        self._config_path: Optional[str] = listing["config_path"]
        self._config_error: Optional[str] = listing["error"]
        self._config_candidates: List[str] = listing["config_candidates"]

        saved = self._settings_store.load(self._repo_root)
        saved_cq = saved.get("cq", {})
        self._profile = self._resolve_initial_profile(
            str(saved_cq.get("profile", ""))
        )
        self._build_profiles = self._settings_store.parse_semicolon_list(
            str(saved_cq.get("build_profiles", ""))
        )

        self._tabs = QTabWidget(self)
        self._tabs.addTab(self._build_basic_tab(), self.tr("基本"))
        self._tabs.addTab(self._build_index_tab(), self.tr("インデックス管理"))
        self._tabs.addTab(self._build_quality_tab(), self.tr("検索品質"))

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(self._tabs)

        self._load_watch_settings(saved)
        self._wire_watch_persistence()
        self._apply_config_availability()
        self._refresh_profile_details()
        self._load_stats()
        self._load_profile_stats_table()
        self._refresh_benchmark_availability()

    def is_config_available(self) -> bool:
        return bool(self._profiles)

    def current_profile(self) -> str:
        return self._profile

    def _resolve_initial_profile(self, saved: str) -> str:
        if saved in self._profiles:
            return saved
        return next(iter(self._profiles), "")

    def _build_basic_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        self._config_banner = QLabel("")
        self._config_banner.setWordWrap(True)
        self._config_banner.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        layout.addWidget(self._config_banner)

        self._profile_combo = QComboBox()
        for name in self._profiles:
            self._profile_combo.addItem(name, name)
        index = self._profile_combo.findData(self._profile)
        if index >= 0:
            self._profile_combo.setCurrentIndex(index)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)

        form = QFormLayout()
        form.addRow(self.tr("Profile"), self._profile_combo)
        layout.addLayout(form)

        description = QLabel(self.tr(
            "profile ごとに別の索引 DB (.cq/index-<profile>.sqlite) を使用します。"
            "索引ルート・除外・最大ファイルサイズは cq の設定ファイルが唯一の情報源であり、"
            "この画面からは変更できません。変更するには設定ファイルを直接編集してください。"
        ))
        description.setWordWrap(True)
        layout.addWidget(description)

        layout.addSpacing(8)
        layout.addWidget(QLabel(self.tr("<b>索引ルート</b>")))
        self._roots_view = QPlainTextEdit()
        self._roots_view.setReadOnly(True)
        self._roots_view.setMaximumHeight(90)
        layout.addWidget(self._roots_view)

        layout.addWidget(QLabel(self.tr("<b>除外パターン</b>")))
        self._excludes_view = QPlainTextEdit()
        self._excludes_view.setReadOnly(True)
        self._excludes_view.setMaximumHeight(140)
        layout.addWidget(self._excludes_view)

        self._max_bytes_label = QLabel("")
        self._max_bytes_label.setWordWrap(True)
        layout.addWidget(self._max_bytes_label)
        layout.addStretch(1)
        return tab

    def _build_index_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)

        layout.addWidget(QLabel(self.tr("<b>インデックスの統計情報</b>")))
        self._stats_label = QLabel(self.tr("読み込み中..."))
        self._stats_label.setWordWrap(True)
        layout.addWidget(self._stats_label)
        layout.addSpacing(8)

        group = QGroupBox(self.tr("インデックス DB の管理"))
        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(8, 8, 8, 8)

        self._btn_incremental_refresh = QPushButton(self.tr("差分更新"))
        self._btn_incremental_refresh.setToolTip(self.tr(
            "内容が変わったソースだけを再索引します（SHA-1 一致はスキップ）。"
        ))
        self._btn_incremental_refresh.clicked.connect(
            lambda: self._start_build(self._profile, rebuild=False)
        )
        self._btn_force_rebuild = QPushButton(self.tr("完全再ビルド"))
        self._btn_force_rebuild.setToolTip(self.tr(
            "既存の索引 DB を削除してから全ソースを再走査します。"
        ))
        self._btn_force_rebuild.clicked.connect(
            lambda: self._start_build(self._profile, rebuild=True)
        )
        self._btn_delete_db = QPushButton(self.tr("DB を削除"))
        self._btn_delete_db.setToolTip(self.tr(
            "現在の profile の索引 DB を削除します。削除後は再ビルドが必要です。"
        ))
        self._btn_delete_db.clicked.connect(self._on_delete_db_clicked)

        button_row = QHBoxLayout()
        for button in (
            self._btn_incremental_refresh,
            self._btn_force_rebuild,
            self._btn_delete_db,
        ):
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button_row.addWidget(button)
        button_row.addStretch(1)
        group_layout.addLayout(button_row)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setRange(0, 0)
        group_layout.addWidget(self._progress)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        group_layout.addWidget(self._result_label)

        group_layout.addSpacing(8)
        group_layout.addWidget(QLabel(self.tr("<b>一括ビルド対象 Profile</b>")))
        self._build_profiles_list = QListWidget()
        self._build_profiles_list.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        selected = set(self._build_profiles)
        for name in self._profiles:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if not selected or name in selected
                else Qt.CheckState.Unchecked
            )
            self._build_profiles_list.addItem(item)
        self._build_profiles_list.setMaximumHeight(120)
        self._build_profiles_list.itemChanged.connect(
            self._on_build_profiles_changed
        )
        group_layout.addWidget(self._build_profiles_list)

        self._btn_bulk_build = QPushButton(self.tr("選択 Profile を一括ビルド"))
        self._btn_bulk_build.clicked.connect(self._on_bulk_build_clicked)
        self._btn_bulk_cancel = QPushButton(
            self.tr("停止（実行中 Profile 完了後）")
        )
        self._btn_bulk_cancel.setEnabled(False)
        self._btn_bulk_cancel.clicked.connect(self._on_bulk_cancel_clicked)
        bulk_row = QHBoxLayout()
        bulk_row.addWidget(self._btn_bulk_build)
        bulk_row.addWidget(self._btn_bulk_cancel)
        bulk_row.addStretch(1)
        group_layout.addLayout(bulk_row)
        self._bulk_message = QLabel("")
        self._bulk_message.setWordWrap(True)
        group_layout.addWidget(self._bulk_message)
        layout.addWidget(group)

        layout.addSpacing(12)
        layout.addWidget(QLabel(self.tr("<b>Profile 別インデックス統計</b>")))
        self._profile_stats_table = QTableWidget()
        self._profile_stats_table.setColumnCount(5)
        self._profile_stats_table.setHorizontalHeaderLabels([
            self.tr("Profile"), self.tr("DB 存在"), self.tr("Files"),
            self.tr("Symbols"), self.tr("最終更新"),
        ])
        self._profile_stats_table.verticalHeader().setVisible(False)
        self._profile_stats_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._profile_stats_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        header = self._profile_stats_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self._profile_stats_table.setMinimumHeight(120)
        layout.addWidget(self._profile_stats_table)

        layout.addSpacing(12)
        layout.addWidget(QLabel(self.tr("<b>言語別インデックス統計</b>")))
        layout.addWidget(QLabel(self.tr(
            "同じパーサ名を複数の言語が共有するため、上のパーサ内訳だけでは言語ごとの"
            "フィデリティを判別できません。この表は選択中の profile の内訳です。"
        )))
        self._language_stats_table = QTableWidget()
        self._language_stats_table.setColumnCount(5)
        self._language_stats_table.setHorizontalHeaderLabels([
            self.tr("言語"), self.tr("Files"), self.tr("Symbols"),
            self.tr("Chunks"), self.tr("パーサ内訳"),
        ])
        self._language_stats_table.verticalHeader().setVisible(False)
        self._language_stats_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._language_stats_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        language_header = self._language_stats_table.horizontalHeader()
        language_header.setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        language_header.setStretchLastSection(True)
        self._language_stats_table.setMinimumHeight(120)
        layout.addWidget(self._language_stats_table)

        layout.addSpacing(16)
        layout.addWidget(QLabel(self.tr("<b>リアルタイム更新</b>")))
        self.cq_watch = TriStateCombo()
        layout.addWidget(LabeledField(
            self.tr("cq リアルタイム更新"),
            self.tr(
                "ソースファイルの追加/更新/削除を OS イベントで検知し索引を逐次更新します。"
                "watchdog 未導入時は自動で無効化されます。"
            ),
            self.cq_watch,
        ))
        self.cq_watch_debounce_ms = QSpinBox()
        self.cq_watch_debounce_ms.setRange(0, 60000)
        self.cq_watch_debounce_ms.setValue(0)
        self.cq_watch_debounce_ms.setSpecialValueText(
            self.tr("（既定 {ms}ms を使用）").format(
                ms=cq_watcher.DEFAULT_DEBOUNCE_MS
            )
        )
        layout.addWidget(LabeledField(
            self.tr("cq watcher デバウンス間隔 (ms)"),
            self.tr("0 のとき cq 既定値を使用します。"),
            self.cq_watch_debounce_ms,
        ))

        layout.addSpacing(16)
        layout.addWidget(QLabel(self.tr("<b>試し検索</b>")))
        self._preview_query = QLineEdit()
        self._preview_query.setPlaceholderText(self.tr("検索語またはシンボル名"))
        self._preview_query.returnPressed.connect(self._on_preview_clicked)
        self._preview_mode = QComboBox()
        self._preview_mode.addItem("auto", "auto")
        for route in cq_search.ROUTES:
            self._preview_mode.addItem(route, route)
        self._preview_top_k = QSpinBox()
        self._preview_top_k.setRange(1, 50)
        self._preview_top_k.setValue(cq_search.DEFAULT_TOP_K)
        self._btn_preview = QPushButton(self.tr("検索"))
        self._btn_preview.clicked.connect(self._on_preview_clicked)
        preview_row = QHBoxLayout()
        preview_row.addWidget(self._preview_query, 1)
        preview_row.addWidget(self._preview_mode)
        preview_row.addWidget(self._preview_top_k)
        preview_row.addWidget(self._btn_preview)
        layout.addLayout(preview_row)
        self._preview_view = QTextBrowser()
        self._preview_view.setMinimumHeight(140)
        layout.addWidget(self._preview_view)
        layout.addStretch(1)
        return tab

    def _build_quality_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel(self.tr("<b>検索品質ベンチマーク</b>")))
        description = QLabel(self.tr(
            "対象リポジトリに cq/golden-queries.json があり、profile が hve または app "
            "の場合に、cq.benchmark を子プロセスとして実行します。"
        ))
        description.setWordWrap(True)
        layout.addWidget(description)
        self._quality_availability = QLabel("")
        self._quality_availability.setWordWrap(True)
        layout.addWidget(self._quality_availability)
        self._btn_benchmark = QPushButton(self.tr("ベンチマークを実行"))
        self._btn_benchmark.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._btn_benchmark.clicked.connect(self._on_benchmark_clicked)
        row = QHBoxLayout()
        row.addWidget(self._btn_benchmark)
        row.addStretch(1)
        layout.addLayout(row)
        self._benchmark_view = QTextBrowser()
        self._benchmark_view.setMinimumHeight(280)
        layout.addWidget(self._benchmark_view, 1)
        return tab

    def _apply_config_availability(self) -> None:
        available = self.is_config_available()
        for widget in (
            self._btn_incremental_refresh,
            self._btn_force_rebuild,
            self._btn_delete_db,
            self._btn_bulk_build,
            self._btn_preview,
            self._profile_combo,
            self._build_profiles_list,
        ):
            widget.setEnabled(available)
        if available:
            self._config_banner.setText(
                self.tr("設定ファイル: {path}").format(
                    path=self._config_path or ""
                )
            )
            return
        candidates = " / ".join(self._config_candidates)
        self._config_banner.setText(self.tr(
            "⚠ cq の設定が読み込めないため索引操作を無効化しています。"
            "リポジトリルートに {candidates} のいずれかを作成し、"
            "[profiles.<名前>] と roots を宣言してください。詳細: {error}"
        ).format(candidates=candidates, error=self._config_error or ""))

    def _refresh_profile_details(self) -> None:
        detail = self._profiles.get(self._profile)
        if detail is None:
            self._roots_view.setPlainText("")
            self._excludes_view.setPlainText("")
            self._max_bytes_label.setText("")
            return
        self._roots_view.setPlainText("\n".join(detail["roots"]))
        self._excludes_view.setPlainText("\n".join(detail["exclude"]))
        self._max_bytes_label.setText(self.tr(
            "最大ファイルサイズ: {bytes:,} bytes（これを超えるファイルは索引しません）"
        ).format(bytes=detail["max_file_bytes"]))

    def _persist_settings(self) -> None:
        settings = self._settings_store.load(self._repo_root)
        settings.setdefault("cq", {})
        settings["cq"]["profile"] = self._profile
        settings["cq"]["build_profiles"] = (
            self._settings_store.serialize_semicolon_list(self._build_profiles)
        )
        self._settings_store.save(self._repo_root, settings)

    def _load_watch_settings(self, settings: dict[str, dict[str, Any]]) -> None:
        options = settings.get("options", {})
        raw_watch = options.get("cq_watch", "")
        if raw_watch is True or raw_watch == "on":
            self.cq_watch.set_tristate(True)
        elif raw_watch is False or raw_watch == "off":
            self.cq_watch.set_tristate(False)
        else:
            self.cq_watch.set_tristate(None)
        try:
            debounce = int(options.get("cq_watch_debounce_ms", 0) or 0)
        except (TypeError, ValueError):
            debounce = 0
        self.cq_watch_debounce_ms.setValue(debounce)

    def _wire_watch_persistence(self) -> None:
        self.cq_watch.currentIndexChanged.connect(self._persist_watch_settings)
        self.cq_watch_debounce_ms.valueChanged.connect(
            self._persist_watch_settings
        )

    def _persist_watch_settings(self, *_args: object) -> None:
        settings = self._settings_store.load(self._repo_root)
        settings.setdefault("options", {})
        watch = self.cq_watch.get_tristate()
        settings["options"]["cq_watch"] = (
            "" if watch is None else ("on" if watch else "off")
        )
        settings["options"]["cq_watch_debounce_ms"] = (
            self.cq_watch_debounce_ms.value()
        )
        self._settings_store.save(self._repo_root, settings)

    def _on_profile_changed(self, _index: int) -> None:
        data = self._profile_combo.currentData()
        self._profile = str(data) if data is not None else ""
        self._persist_settings()
        self._refresh_profile_details()
        self._load_stats()
        self._refresh_benchmark_availability()

    def _on_build_profiles_changed(self, _item: QListWidgetItem) -> None:
        self._build_profiles = self._checked_build_profiles()
        self._persist_settings()

    def _load_stats(self) -> None:
        if not self._profile:
            self._stats_label.setText(self.tr("profile が未解決です。"))
            self._language_stats_table.setRowCount(0)
            return
        stats = index_service.get_index_stats(self._repo_root, self._profile)
        self._stats_label.setText(self._format_stats(stats))
        self._fill_language_stats(stats)

    def _fill_language_stats(self, stats: dict) -> None:
        rows = sorted((stats.get("by_lang") or {}).items())
        self._language_stats_table.setRowCount(len(rows))
        for row, (lang, entry) in enumerate(rows):
            by_parser = ", ".join(
                f"{name}={count}"
                for name, count in sorted(entry["by_parser"].items())
            )
            values = [
                lang,
                f"{entry['files']:,}",
                f"{entry['symbols']:,}",
                f"{entry['chunks']:,}",
                by_parser,
            ]
            for column, value in enumerate(values):
                self._language_stats_table.setItem(
                    row, column, QTableWidgetItem(value)
                )

    def _format_stats(self, stats: dict) -> str:
        if stats.get("error"):
            return self.tr("統計を取得できません: {error}").format(
                error=stats["error"]
            )
        if not stats["db_exists"]:
            return self.tr(
                "索引 未作成\nDB: {db}\n「差分更新」でビルドしてください。"
            ).format(db=stats["db_path"])
        parser = ", ".join(
            f"{key}={value}" for key, value in sorted(stats["by_parser"].items())
        )
        counts = "  ".join(
            f"{table}={stats[table]:,}" for table in cq_store.STATS_TABLES
        )
        return counts + "\n" + self.tr(
            "パーサ内訳: {parser}\nスキーマ v{version}\n最終更新: {mtime}\nDB: {db}"
        ).format(
            parser=parser or "-",
            version=stats["schema_version"],
            mtime=stats["db_mtime"],
            db=stats["db_path"],
        )

    def _load_profile_stats_table(self) -> None:
        rows = index_service.get_index_stats_all_profiles(self._repo_root)
        self._profile_stats_table.setRowCount(len(rows))
        for row, (name, stats) in enumerate(rows.items()):
            values = [
                name,
                "○" if stats["db_exists"] else "—",
                f"{stats['files']:,}",
                f"{stats['symbols']:,}",
                stats["db_mtime"],
            ]
            for column, value in enumerate(values):
                self._profile_stats_table.setItem(
                    row, column, QTableWidgetItem(value)
                )

    def _set_index_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self._btn_incremental_refresh,
            self._btn_force_rebuild,
            self._btn_delete_db,
            self._btn_bulk_build,
        ):
            button.setEnabled(enabled and self.is_config_available())

    def _start_build(self, profile: str, *, rebuild: bool) -> None:
        if not profile or self._build_thread is not None:
            return
        self._set_index_buttons_enabled(False)
        self._progress.setVisible(True)
        self._building_profile = profile
        self._result_label.setText(
            self.tr("{profile} を索引中...").format(profile=profile)
        )
        thread = CqIndexBuildThread(
            repo_root=self._repo_root,
            profile=profile,
            rebuild=rebuild,
            parent=self,
        )
        thread.succeeded.connect(self._on_build_succeeded)
        thread.failed.connect(self._on_build_failed)
        thread.finished.connect(self._on_build_finished)
        self._build_thread = thread
        thread.start()

    def _on_build_succeeded(self, report: dict) -> None:
        self._result_label.setText(self.tr(
            "完了: indexed={indexed} skipped={skipped} pruned={pruned} "
            "degraded={degraded} errors={errors} symbols={symbols} "
            "chunks={chunks} ({elapsed_ms} ms)"
        ).format(**report))

    def _on_build_failed(self, message: str) -> None:
        self._result_label.setText(
            self.tr("失敗: {message}").format(message=message)
        )
        if self._bulk_total:
            self._bulk_failed.append((self._building_profile, message))

    def _on_build_finished(self) -> None:
        self._build_thread = None
        self._building_profile = ""
        self._progress.setVisible(False)
        self._set_index_buttons_enabled(True)
        self._load_stats()
        self._load_profile_stats_table()
        if self._bulk_total:
            self._run_next_bulk_build()

    def _on_delete_db_clicked(self) -> None:
        answer = QMessageBox.question(
            self,
            self.tr("索引 DB の削除"),
            self.tr("profile '{profile}' の索引 DB を削除しますか？").format(
                profile=self._profile
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = index_service.delete_index_db(
                self._repo_root, self._profile
            )
        except OSError as exc:
            self._result_label.setText(
                self.tr("削除に失敗しました: {error}").format(error=exc)
            )
            return
        self._result_label.setText(
            self.tr("{count} 件のファイルを削除しました。").format(
                count=len(removed)
            )
            if removed
            else self.tr("削除対象の索引 DB はありませんでした。")
        )
        self._load_stats()
        self._load_profile_stats_table()

    def _checked_build_profiles(self) -> List[str]:
        return [
            self._build_profiles_list.item(index).text()
            for index in range(self._build_profiles_list.count())
            if self._build_profiles_list.item(index).checkState()
            == Qt.CheckState.Checked
        ]

    def _on_bulk_build_clicked(self) -> None:
        if self._build_thread is not None or self._bulk_total:
            return
        targets = self._checked_build_profiles()
        if not targets:
            self._bulk_message.setText(
                self.tr("対象 Profile が選択されていません。")
            )
            return
        self._bulk_queue = list(targets)
        self._bulk_total = len(targets)
        self._bulk_failed = []
        self._bulk_cancel = False
        self._btn_bulk_cancel.setEnabled(True)
        self._run_next_bulk_build()

    def _run_next_bulk_build(self) -> None:
        if self._bulk_cancel or not self._bulk_queue:
            self._finalize_bulk_build()
            return
        profile = self._bulk_queue.pop(0)
        done = self._bulk_total - len(self._bulk_queue)
        self._bulk_message.setText(self.tr(
            "一括ビルド {done}/{total}: {profile}"
        ).format(done=done, total=self._bulk_total, profile=profile))
        self._start_build(profile, rebuild=False)

    def _on_bulk_cancel_clicked(self) -> None:
        self._bulk_cancel = True
        self._btn_bulk_cancel.setEnabled(False)

    def _finalize_bulk_build(self) -> None:
        total = self._bulk_total
        failed = list(self._bulk_failed)
        self._bulk_total = 0
        self._bulk_queue = []
        self._bulk_failed = []
        self._btn_bulk_cancel.setEnabled(False)
        if not total:
            return
        if failed:
            detail = ", ".join(
                f"{name}: {message}" for name, message in failed
            )
            self._bulk_message.setText(self.tr(
                "一括ビルド終了（{failed}/{total} 失敗）: {detail}"
            ).format(failed=len(failed), total=total, detail=detail))
        else:
            self._bulk_message.setText(
                self.tr("一括ビルド完了（{total} Profile）").format(total=total)
            )

    def _on_preview_clicked(self) -> None:
        query = self._preview_query.text().strip()
        if not query or self._preview_thread is not None or not self._profile:
            return
        self._btn_preview.setEnabled(False)
        self._preview_view.setPlainText(self.tr("検索中..."))
        thread = CqSearchPreviewThread(
            repo_root=self._repo_root,
            profile=self._profile,
            query=query,
            mode=str(self._preview_mode.currentData()),
            top_k=self._preview_top_k.value(),
            parent=self,
        )
        thread.succeeded.connect(self._on_preview_succeeded)
        thread.failed.connect(self._on_preview_failed)
        thread.finished.connect(self._on_preview_finished)
        self._preview_thread = thread
        thread.start()

    def _on_preview_succeeded(self, result: dict) -> None:
        hits = result["hits"]
        if not hits:
            self._preview_view.setPlainText(self.tr("ヒットなし"))
            return
        lines: list[str] = []
        for hit in hits:
            start, end = hit["lines"]
            lines.append(
                f"{hit['path']}:{start}-{end}  "
                f"[{hit['route']}] {hit['score']}"
            )
            snippet = (hit.get("snippet") or "").strip()
            if snippet:
                lines.append(snippet)
            lines.append("")
        staleness = result.get("staleness")
        if staleness:
            lines.append(self.tr(
                "⚠ 索引が古い可能性があります: {info}"
            ).format(info=staleness))
        self._preview_view.setPlainText("\n".join(lines))

    def _on_preview_failed(self, message: str) -> None:
        self._preview_view.setPlainText(
            self.tr("検索に失敗しました: {message}").format(message=message)
        )

    def _on_preview_finished(self) -> None:
        self._preview_thread = None
        self._btn_preview.setEnabled(self.is_config_available())

    def _benchmark_is_available(self) -> bool:
        return (
            self.is_config_available()
            and self._profile in golden_eval.PROFILES
            and (self._repo_root / "cq" / "golden-queries.json").is_file()
        )

    def _refresh_benchmark_availability(self) -> None:
        available = self._benchmark_is_available()
        self._btn_benchmark.setEnabled(
            available and self._benchmark_thread is None
        )
        self._quality_availability.setText(
            ""
            if available
            else self.tr(
                "このリポジトリ/profile には対応するゴールデンクエリがないため、"
                "ベンチマークは利用できません。索引管理と試し検索は利用できます。"
            )
        )

    def _on_benchmark_clicked(self) -> None:
        if self._benchmark_thread is not None or not self._benchmark_is_available():
            return
        self._btn_benchmark.setEnabled(False)
        thread = CqBenchmarkThread(
            repo_root=self._repo_root,
            profile=self._profile,
            parent=self,
        )
        self._benchmark_view.setPlainText(self.tr(
            "実行中: {command}"
        ).format(command=" ".join(thread.command())))
        thread.succeeded.connect(self._benchmark_view.setPlainText)
        thread.failed.connect(self._on_benchmark_failed)
        thread.finished.connect(self._on_benchmark_finished)
        self._benchmark_thread = thread
        thread.start()

    def _on_benchmark_failed(self, message: str) -> None:
        self._benchmark_view.setPlainText(self.tr(
            "ベンチマークに失敗しました:\n{message}"
        ).format(message=message))

    def _on_benchmark_finished(self) -> None:
        self._benchmark_thread = None
        self._refresh_benchmark_availability()
