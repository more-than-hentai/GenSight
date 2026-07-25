from . import auth, library, media, scan, system, trash

ALL = [
    system.router,
    scan.router,
    library.router,
    media.router,
    trash.router,
    auth.router,
]
