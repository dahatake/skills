# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- リポジトリルートの配布物を Agent Plugins v1.0.0 に対応。標準スキーマを宣言する
  `plugin.json`、`mcp.json`、マニフェスト検証スクリプト、Agent Skills 公式
  validator を使う CI を追加した。

### Changed

- `code-query` / `markdown-query` の Skill frontmatter を Agent Skills 仕様に適合。
  Claude Code 用マニフェストは、ポータブル `plugin.json` の閉じたスキーマを
  壊さないよう互換メタデータとして個別生成する。

### Removed

- `tool-search` キットと関連する全て（`tool-search/`、`docs/tool-search.md`、
  `README.md` の記述、CI のスモークテスト、`.gitattributes` / `.gitignore` /
  `scripts/refresh-kit-manifest.py` の参照）。

## [0.2.0] - 2026-08-13

### Added

- 配布キット方式の 3 ツールを収録。`markdown-query/`（Markdown 横断検索）、
  `code-query/`（ソースコード検索）、`tool-search/`（Copilot SDK 向けツール検索
  ライブラリ）。各キットは `install.ps1` / `install.sh` / `install.py` を同梱し、
  単体でも導入できる。
- `.gitattributes`。シェルスクリプトを LF に固定し、`KIT-VERSION.json` が
  ハッシュ検証する配布物は改行変換の対象外にする。
- `scripts/sync-plugin-assets.py`。`skills/` と `.claude-plugin/plugin.json` を
  正本（各キットの `skill/` と `plugin.json`）から生成する。`--check` で差分検出。
- `docs/` に 3 ツールの技術ドキュメントを収録。
- `.github/workflows/verify.yml`。Linux 上でキットのハッシュ検証・生成物の同期確認・
  改行コードと実行権限の検査・3 キットのスモークインストールを実行する。

### Changed

- `README.md` を全面的に書き直し。3 ツールの選択・前提条件・OS 別インストール・
  インストール後の設定・動作確認・エージェント連携・更新手順までを 1 本の導線に
  まとめ、詳細は `docs/` の各ツールページへ委譲する構成にした。
- `docs/` をツール別に再編。`docs/markdown-query.md` / `docs/code-query.md` /
  `docs/tool-search.md` を書き下ろし、上流リポジトリの users-guide をそのまま
  置いていた重複ファイルを削除した（同内容は各キットの `docs/` が保持する）。
- 各キットの同梱ドキュメントのリンクを、同梱の実体（`vendor/`, `skill/`）へ
  向け直した。キット内リンク切れは 156 件から 30 件へ減少（残りは上流
  リポジトリでのみ解決するパスで、`GETTING-STARTED.md` に明記済み）。
- 配布キットの不具合修正と可搬性改善。詳細は各キットの `KIT-VERSION.json` の
  `local_patches` を参照。
- プラグイン定義（`plugin.json` / `.claude-plugin/*` / `gemini-extension.json` /
  `apm.yml`）の版を 0.2.0 に更新し、2 つの Skill を反映。

### Fixed

- `tool-search` が `<repo>/.toolsearch/policy.json` を実際に採用するようになった。
  従来は `policy` / `eval` / `build_session_toolset` のいずれも `repo_root` を
  渡しておらず、リポジトリ側に置いたポリシーが無視されていた。
- `code-query` をソースコードの無いリポジトリへ導入したときに、トレースバックでは
  なく理由のわかるメッセージで停止するようになった。`cq.toml` が無い状態の
  `stats` / `search` も、上流プロファイル名の DB を探さず設定不備を報告する。
- `install.ps1` に `-Version` / `-Verify` を追加。整合性確認が UTF-8 出力の経路を
  通るため、Windows で結果が読めるようになった。

### Removed

- 旧経路の資産。ルートの `mdq/` パッケージ（キットの `vendor/mdq` に対して版が
  古く内容も乖離していた）、`setup/`、`tools/`、`dist/`、`mdq.egg-info/`、
  `pyproject.toml`、`MANIFEST.in`、`RELEASE.md`。`mdq` の PyPI 配布は行わず、
  キット同梱の `vendor/` を唯一の実装とする。
- `docs/tool-search-guide.md`。Microsoft Foundry Toolbox の tool search に関する
  上流ドキュメントで、本リポジトリの `tool-search` とは別機能のため。
- `skills/markdown-query/references/repo-specific/`（`hve-defaults.md` /
  `hve-integration.md`）。上流リポジトリ固有のリファレンスで、キットの
  `skill/references/` には含まれないため生成対象から外れた。

## [0.1.0]

### Changed

- `README.md` を大幅に拡充し、`markdown-query` の概要・アーキテクチャ・
  インストール・使用方法・Chunking Strategy・クエリルーティング・索引データ構造・
  ベンチマーク・利用統計を一本化。
