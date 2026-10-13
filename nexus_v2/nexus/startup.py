"""Windows auto-start via a visible per-user Scheduled Task (no elevation, no hidden persistence), created from XML so that:
 * ExecutionTimeLimit is unlimited (the schtasks CLI default of 72 h would end a 7-day run),
 * it runs on battery and is not stopped on battery, starts late if missed, and ignores duplicate triggers.
The registration is VERIFIED by querying the task back. On non-Windows hosts nothing is claimed as configured."""
import os, pathlib, subprocess, sys, tempfile
from xml.sax.saxutils import escape
from . import config

TASK = "NEXUS_V2_Research"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def task_xml(root=ROOT, user=None):
    user = user or (f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\"))
    cmd = str(pathlib.PureWindowsPath(root) / "START_NEXUS.bat") if sys.platform.startswith("win") else str(root / "START_NEXUS.bat")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts the NEXUS V2 research runtime (research only; live trading disabled).</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId><Delay>PT1M</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowHardTerminate>true</AllowHardTerminate>
    <Hidden>false</Hidden>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(cmd)}</Command><Arguments>--autostart</Arguments><WorkingDirectory>{escape(str(root))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def run(install: bool) -> int:
    if not sys.platform.startswith("win"):
        print(f"NOT CONFIGURED: automatic startup is only implemented for Windows (this host: {sys.platform}). Nothing was installed. "
              f"Run INSTALL_AUTOSTART.bat on the Windows PC.")
        return 2
    if not install:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        return r.returncode
    xml = pathlib.Path(tempfile.gettempdir()) / "nexus_task.xml"
    xml.write_text(task_xml(), encoding="utf-16")
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK, "/XML", str(xml), "/F"], capture_output=True, text=True)
    print((r.stdout or r.stderr).strip())
    if r.returncode != 0:
        print("Task registration FAILED (a managed PC may forbid it); NEXUS will still run when started manually.")
        return r.returncode
    q = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"], capture_output=True, text=True)
    ok = q.returncode == 0 and "START_NEXUS.bat" in q.stdout and "PT0S" in q.stdout
    print("VERIFIED: task is registered with START_NEXUS.bat and no execution time limit." if ok else "NOT VERIFIED: could not read the registered task back correctly.")
    return 0 if ok else 1
