"""stream_tree sends the tree in bounded chunks and reproduces it exactly."""
import filecmp, os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import woa_installer as w


class FakePhone:
    dry = False

    def __init__(self):
        self.procs = 0
        self.syncs = 0

    def stream_in(self, cmd):
        self.procs += 1
        return subprocess.Popen(cmd.split(), stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    meminfo = ""

    def su(self, cmd, check=True, mutating=False):
        if cmd == "sync":
            self.syncs += 1
        if "MemAvailable" in cmd:
            return self.meminfo
        return ""


def check_chunk_size():
    ph = FakePhone()
    ph.meminfo = "MemAvailable:    9583244 kB"
    assert w.chunk_size(ph) == w.STREAM_CHUNK                    # plenty of RAM: the cap
    ph.meminfo = "MemAvailable:    1048576 kB"
    assert w.chunk_size(ph) == 256 * 1024 * 1024                 # a quarter of free RAM
    ph.meminfo = "MemAvailable:      40000 kB"
    assert w.chunk_size(ph) == w.STREAM_CHUNK_MIN                # never below the floor
    ph.meminfo = "garbage"
    assert w.chunk_size(ph) == w.STREAM_CHUNK                    # unreadable: the cap


def main():
    check_chunk_size()
    if sys.platform == "win32":
        print("stream test skipped (needs a POSIX tar)")
        return
    with tempfile.TemporaryDirectory() as t:
        src, dst = Path(t, "src"), Path(t, "dst")
        dst.mkdir()
        (src / "a" / "b").mkdir(parents=True)
        (src / "empty").mkdir()
        for i in range(10):
            (src / "a" / ("f%d.bin" % i)).write_bytes(os.urandom(300_000))
        (src / "a" / "b" / "deep.txt").write_text("x" * 10)
        (src / "top.txt").write_text("hello")
        w.STREAM_CHUNK = 1_000_000
        ph = FakePhone()
        w.stream_tree(ph, str(src), str(dst), "test")
        assert ph.procs >= 3, ph.procs          # several bounded tar runs, not one
        assert ph.syncs == ph.procs, (ph.procs, ph.syncs)

        def same(c):
            assert not c.left_only and not c.right_only and not c.diff_files, \
                (c.left_only, c.right_only, c.diff_files)
            for sub in c.subdirs.values():
                same(sub)
        same(filecmp.dircmp(src, dst))
        assert (dst / "empty").is_dir()
    print("stream OK")


main()
