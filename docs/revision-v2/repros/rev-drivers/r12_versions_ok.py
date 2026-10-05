import sys; sys.path.insert(0, ".")
from vms.ops.security.advisories import version_table, evaluate, dahua_version_tuple, firmware_build_date
from vms.vendors.hikvision import build_date
from vms.vendors.dahua import firmware_date
t = version_table().table
for fw in ["2.840.0000000.13.R,build:2022-01-01", "2.820.0000000.18.R,build:2021-07-04", "2.820.0000000.18.R,build:2021-07-05",
           "2.820.0000000.18.R", "2.820.0000000.17.R", "V2.800.0000016.0.R, build : 2020-03-24", "2.820.0000000.18.R.210705", ""]:
    print(f"{fw!r:45}", dahua_version_tuple(fw), [m.verdict for m in evaluate(t, "dahua", "DHI-IPC-HFW5442E-ZE", fw, firmware_date(fw))])
for s in ["build 210812", "V5.7.3 build 220112", "build 20210628", "2021-06-28", "build 211332"]:
    print(f"hik {s!r:24} -> {build_date(s)!r}  audit={firmware_build_date(s)}")
