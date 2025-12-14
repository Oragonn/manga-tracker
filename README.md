go into the venv
manga-tracker\venv\Scripts\Activate.ps1

Start the server
python -m backend.main

Check backup status
bashpython -c "from backend.backup_manager import BackupManager; import json; bm = BackupManager(); print(json.dumps(bm.get_backup_stats(), indent=2, default=str))"

Create manual backup
bashpython -c "from backend.backup_manager import BackupManager; bm = BackupManager(); bm.create_backup()"

Restore from backup
python -c "from backend.backup_manager import BackupManager; bm = BackupManager(); bm.restore_backup('tracker_backup_20241213_180000.db.gz')"