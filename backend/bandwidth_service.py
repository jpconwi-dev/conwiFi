"""Per-client speed limits with Linux tc (HTB). Download = LAN egress; upload = IFB ingress mirror.
Commands are generated as plain lists so they can be unit-tested without root."""
import subprocess

IFB = "ifb0"


def minor_for(client_id: int) -> str:
    return "%x" % (100 + (client_id % 60000))


def base_commands(lan: str) -> list[list[str]]:
    return [
        ["ip", "link", "add", IFB, "type", "ifb"],
        ["ip", "link", "set", IFB, "up"],
        ["tc", "qdisc", "replace", "dev", lan, "root", "handle", "1:", "htb", "default", "1"],
        ["tc", "class", "replace", "dev", lan, "parent", "1:", "classid", "1:1", "htb", "rate", "1000mbit"],
        ["tc", "qdisc", "replace", "dev", lan, "handle", "ffff:", "ingress"],
        ["tc", "filter", "replace", "dev", lan, "parent", "ffff:", "protocol", "ip", "u32",
         "match", "u32", "0", "0", "action", "mirred", "egress", "redirect", "dev", IFB],
        ["tc", "qdisc", "replace", "dev", IFB, "root", "handle", "2:", "htb", "default", "1"],
        ["tc", "class", "replace", "dev", IFB, "parent", "2:", "classid", "2:1", "htb", "rate", "1000mbit"],
    ]


def limit_commands(lan: str, client_id: int, ip: str, down_kbps: int, up_kbps: int) -> list[list[str]]:
    m = minor_for(client_id)
    pr = str(1000 + int(m, 16) % 60000)
    cmds = []
    if down_kbps > 0:
        cmds += [
            ["tc", "class", "replace", "dev", lan, "parent", "1:1", "classid", "1:" + m,
             "htb", "rate", f"{down_kbps}kbit", "ceil", f"{down_kbps}kbit"],
            ["tc", "filter", "replace", "dev", lan, "parent", "1:", "protocol", "ip", "prio", pr,
             "u32", "match", "ip", "dst", ip + "/32", "flowid", "1:" + m],
        ]
    if up_kbps > 0:
        cmds += [
            ["tc", "class", "replace", "dev", IFB, "parent", "2:1", "classid", "2:" + m,
             "htb", "rate", f"{up_kbps}kbit", "ceil", f"{up_kbps}kbit"],
            ["tc", "filter", "replace", "dev", IFB, "parent", "2:", "protocol", "ip", "prio", pr,
             "u32", "match", "ip", "src", ip + "/32", "flowid", "2:" + m],
        ]
    return cmds


def remove_commands(lan: str, client_id: int) -> list[list[str]]:
    m = minor_for(client_id)
    pr = str(1000 + int(m, 16) % 60000)
    return [
        ["tc", "filter", "del", "dev", lan, "parent", "1:", "prio", pr],
        ["tc", "class", "del", "dev", lan, "classid", "1:" + m],
        ["tc", "filter", "del", "dev", IFB, "parent", "2:", "prio", pr],
        ["tc", "class", "del", "dev", IFB, "classid", "2:" + m],
    ]


class BandwidthManager:
    def __init__(self, lan: str, simulation: bool, sim_log=None):
        self.lan, self.sim, self.sim_log = lan, simulation, sim_log
        self.applied: dict[int, tuple] = {}
        self.errors: list[str] = []

    def _run(self, cmds, ignore_errors=False):
        for c in cmds:
            if self.sim:
                if self.sim_log:
                    with open(self.sim_log, "a") as fh:
                        fh.write(" ".join(c) + "\n")
                continue
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode != 0 and not ignore_errors:
                self.errors.append(f"{' '.join(c)}: {r.stderr.strip()[:200]}")

    def reset(self):
        self.applied.clear()
        if not self.sim:
            subprocess.run(["tc", "qdisc", "del", "dev", self.lan, "root"], capture_output=True)
            subprocess.run(["tc", "qdisc", "del", "dev", self.lan, "ingress"], capture_output=True)
        self._run([c for c in base_commands(self.lan)][:1], ignore_errors=True)  # ifb may already exist
        self._run(base_commands(self.lan)[1:])

    def sync(self, wanted: dict[int, tuple]):
        """wanted: client_id -> (ip, down_kbps, up_kbps). Applies only what changed."""
        for cid in list(self.applied):
            if cid not in wanted:
                self._run(remove_commands(self.lan, cid), ignore_errors=True)
                del self.applied[cid]
        for cid, spec in wanted.items():
            if self.applied.get(cid) == spec:
                continue
            ip, down, up = spec
            if self.applied.get(cid):
                self._run(remove_commands(self.lan, cid), ignore_errors=True)
            if down > 0 or up > 0:
                self._run(limit_commands(self.lan, cid, ip, down, up))
            self.applied[cid] = spec
