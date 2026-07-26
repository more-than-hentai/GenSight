from . import audit_log, auth, library, media, scan, system, trash

ALL = [
    system.router,
    scan.router,
    library.router,
    media.router,
    trash.router,
    audit_log.router,
    auth.router,
]
