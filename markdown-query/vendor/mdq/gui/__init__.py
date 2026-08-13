"""mdq.gui - Management GUI for the markdown-query Skill.

This package contains a self-contained Qt (PySide6) settings panel and a
standalone window that can be launched independently of the HVE GUI.

It is the single implementation of the panel (FR-GUI-05). The HVE settings
window injects its own settings backend through
``hve.gui.mdq_settings_section``; the distribution kit ships this package via
``vendor/mdq/`` and its launcher scripts put that directory on ``sys.path``.
"""
