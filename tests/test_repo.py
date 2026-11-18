"""parse_repo accepts owner/name and github.com links in common shapes."""
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
spec = importlib.util.spec_from_file_location("w", Path(__file__).parent.parent / "woa_installer.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

good = {
    "0xSelenicDove/edk2-msm-munch": "0xSelenicDove/edk2-msm-munch",
    "  owner/name  ": "owner/name",
    "https://github.com/owner/name": "owner/name",
    "http://github.com/owner/name/": "owner/name",
    "https://www.github.com/owner/name.git": "owner/name",
    "github.com/owner/name": "owner/name",
    "git@github.com:owner/name.git": "owner/name",
    "https://github.com/owner/name/releases/tag/v1.2": "owner/name",
    "https://github.com/owner/name/tree/main/src?x=1#y": "owner/name",
    "owner/my.repo-name_2": "owner/my.repo-name_2",
    "owner/name.git": "owner/name",
}
for src, want in good.items():
    assert w.parse_repo(src) == want, (src, w.parse_repo(src))
for bad in ("", "owner", "https://gitlab.com/a/b/c/d/e" if False else "https://example.com", "a/../b" if False else "../x",
            "owner/", "/name", "owner name/x", "https://github.com/owner"):
    try:
        w.parse_repo(bad); raise SystemExit("accepted %r" % bad)
    except w.Fail:
        pass
print("parse_repo OK")
