"""unattend.xml generation: well-formed, right passes/components, escaping, validation."""
import importlib.util, tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

spec = importlib.util.spec_from_file_location("w", Path(__file__).parent.parent / "woa_installer.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
NS = {"u": "urn:schemas-microsoft-com:unattend"}

xml = w.build_unattend("Alice", 'p&ss<"w>', "MY-TAB", "de-DE", "W. Europe Standard Time", drivers=True)
root = ET.fromstring(xml)                                   # well-formed
passes = {s.get("pass"): s for s in root.findall("u:settings", NS)}
assert set(passes) == {"specialize", "oobeSystem"}
assert all(c.get("processorArchitecture") == "arm64" for s in passes.values() for c in s)
cmds = [p.text for p in passes["specialize"].iter("{%s}Path" % NS["u"])]
assert any("BypassNRO" in c for c in cmds) and any("DisablePrivacyExperience" in c for c in cmds)
oobe = root.find(".//u:OOBE", NS)
for tag in ("HideEULAPage", "HideOnlineAccountScreens", "HideWirelessSetupInOOBE", "HideLocalAccountScreen"):
    assert oobe.find("u:" + tag, NS).text == "true", tag
assert oobe.find("u:ProtectYourPC", NS).text == "3"
assert root.find(".//u:LocalAccount/u:Name", NS).text == "Alice"
assert root.find(".//u:LocalAccount/u:Password/u:Value", NS).text == 'p&ss<"w>'   # escaped round-trip
assert root.find(".//u:AutoLogon/u:Username", NS).text == "Alice"
assert root.find(".//u:ComputerName", NS).text == "MY-TAB"
assert root.find(".//u:UILanguage", NS).text == "de-DE"
assert "pnputil" in root.find(".//u:CommandLine", NS).text
orders = [int(o.text) for o in passes["specialize"].iter("{%s}Order" % NS["u"])]
assert orders == list(range(1, len(orders) + 1))

for bad in (dict(user=""), dict(user="a/b"), dict(user="x" * 21), dict(hostname="bad name"),
            dict(hostname="1234"), dict(locale="english")):
    try:
        w.build_unattend(**bad); raise SystemExit("accepted %r" % bad)
    except w.Fail:
        pass

d = Path(tempfile.mkdtemp()); w.write_unattend(d, xml)
assert (d / "Windows/Panther/unattend.xml").read_text(encoding="utf-8") == xml
assert (d / "Windows/Panther/Unattend/Unattend.xml").exists()
assert b"<drivers" not in xml.encode() and "pnputil" not in w.build_unattend()   # no driver step by default
print("unattend OK")
