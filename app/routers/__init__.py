from . import (admin_library, audit_log, auth, library, media, scan, system,
               trash)

ALL = [
    system.router,
    scan.router,
    library.router,
    media.router,
    trash.router,
    audit_log.router,
    admin_library.router,
    auth.router,
]
