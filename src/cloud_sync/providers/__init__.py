"""Per-provider auth + change-detection.

Phase 2:
  - google.py — OAuth code flow, refresh token, drive.changes.list watcher
  - icloud.py — Apple ID + app-specific password, full-listing diff watcher
  - dropbox.py — OAuth code flow, /files/list_folder/longpoll watcher
"""
