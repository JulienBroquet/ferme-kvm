# -*- coding: utf-8 -*-
"""Fausses machines pour tester le moteur sans SSH ni libvirt : chaque commande est enregistrée, et
quelques commandes (virsh, qemu-img, rsync, cat >) sont simulées avec un petit état en mémoire."""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import labferme as L  # noqa: E402


class FauxHote(L.Hote):
    """Hote dont run() ne contacte personne. `etat` simule libvirt et le système de fichiers."""

    def __init__(self, nom, ip="10.0.0.1", utilisateur="enseignant", mode="sudo", local=False, **kw):
        super().__init__(nom, ip, utilisateur, local=local, **kw)
        self._mode = mode
        self._home = f"/home/{utilisateur}"
        self.journal = []                       # [(commande, entree)]
        self.reponses = []                      # [(regex, (rc, out, err) ou fonction(cmd, entree))]
        self.echecs = []                        # regex : la commande correspondante échoue (code 1)
        self.etat = {"reseaux": {}, "domaines": {}, "actifs": set(), "autostart": set(),
                     "ecrits": {}, "disques": {}, "existe": set(), "libre_ko": 10 * 1024 ** 2}

    # --- journal ---------------------------------------------------------------------------------
    def commandes(self, motif=""):
        return [c for c, _ in self.journal if re.search(motif, c)]

    def fermer(self):
        pass

    # --- exécution simulée -----------------------------------------------------------------------
    def run(self, cmd, entree=None, verifier=True):
        self.journal.append((cmd, entree))
        rc, out, err = self._repondre(cmd, entree)
        if verifier and rc != 0:
            raise L.Erreur(f"{self.nom} : échec de « {cmd[:150]} » (code {rc}) : {(err or out).strip()}")
        return rc, out, err

    def _repondre(self, cmd, entree):
        for motif in self.echecs:
            if re.search(motif, cmd):
                return 1, "", "échec simulé"
        for motif, rep in self.reponses:
            if re.search(motif, cmd):
                return rep(cmd, entree) if callable(rep) else rep
        et = self.etat
        m = re.search(r"qemu:///system (.*)$", cmd)
        if m:
            return self._virsh(m.group(1).strip(), entree)
        if cmd.startswith("mktemp"):
            return 0, f"/tmp/labferme-{len(et['ecrits'])}.xml\n", ""
        m = re.match(r"cat > (\S+)$", cmd)
        if m:
            et["ecrits"][m.group(1).strip("'")] = entree
            return 0, "", ""
        if "echo $HOME" in cmd:
            return 0, self._home + "\n", ""
        if cmd.startswith("ip -o link"):
            return 0, "1: lo: <LOOPBACK>\n2: eth0: <UP>\n", ""
        if cmd.startswith("ip -o -4 addr"):
            return 0, "2: eth0    inet 192.168.100.11/24 brd 192.168.100.255 scope global eth0\n", ""
        if re.search(r"df -P -k", cmd):
            return 0, f"/dev/sda1 100000000 1000 {et['libre_ko']} 1% /\n", ""
        m = re.search(r"qemu-img info (?:-U )?(?:--backing-chain )?--output=json (\S+)$", cmd) or \
            re.search(r"qemu-img info .*--output=json (\S+)$", cmd)
        if m:
            chemin = m.group(1).strip("'")
            d = et["disques"].get(chemin, {"format": "qcow2", "actual-size": 1024 ** 3})
            if "--backing-chain" in cmd:
                return 0, json.dumps(d.get("chaine") or [dict(d, filename=chemin)]), ""
            return 0, json.dumps(d), ""
        m = re.match(r"test -[efd] (\S+)$", cmd)
        if m:
            return (0 if m.group(1).strip("'") in et["existe"] else 1), "", ""
        return 0, "", ""

    def _virsh(self, a, entree):
        et = self.etat
        noms = lambda xml: re.search(r"<name>([^<]+)</name>", xml).group(1)
        if a == "net-list --name":
            return 0, "\n".join(n for n in et["reseaux"] if n in et.get("reseaux_actifs", et["reseaux"])) + "\n", ""
        if a == "net-list --all --name":
            return 0, "\n".join(et["reseaux"]) + "\n", ""
        if a == "net-list --all --autostart --name":
            return 0, "", ""
        m = re.match(r"net-dumpxml (\S+)$", a)
        if m:
            return 0, et["reseaux"][m.group(1)], ""
        if a == "list --all --name":
            return 0, "\n".join(et["domaines"]) + "\n", ""
        if a == "list --name":
            return 0, "\n".join(n for n in et["domaines"] if n in et["actifs"]) + "\n", ""
        if a == "list --all --autostart --name":
            return 0, "\n".join(sorted(et["autostart"])) + "\n", ""
        m = re.match(r"dumpxml --inactive (\S+)$", a)
        if m:
            return 0, et["domaines"][m.group(1)], ""
        m = re.match(r"(shutdown|destroy) (\S+)$", a)
        if m:
            et["actifs"].discard(m.group(2))
            return 0, "", ""
        m = re.match(r"start (\S+)$", a)
        if m:
            et["actifs"].add(m.group(1))
            return 0, "", ""
        m = re.match(r"(net-)?define (\S+)$", a)
        if m:
            xml = et["ecrits"][m.group(2).strip("'")]
            (et["reseaux"] if m.group(1) else et["domaines"])[noms(xml)] = xml
            return 0, "", ""
        m = re.match(r"(net-)?undefine (?:--nvram )?(\S+)$", a)
        if m:
            (et["reseaux"] if m.group(1) else et["domaines"]).pop(m.group(2), None)
            return 0, "", ""
        return 0, "", ""


def xml_reseau(nom, pont="virbr1", ip="10.10.1.1", masque="255.255.255.0"):
    return (f"<network><name>{nom}</name><uuid>aaaa</uuid><forward mode='nat'/>"
            f"<bridge name='{pont}' stp='on' delay='0'/><mac address='52:54:00:00:00:01'/>"
            f"<ip address='{ip}' netmask='{masque}'><dhcp><range start='{ip[:-1]}10' end='{ip[:-1]}50'/></dhcp>"
            f"</ip></network>")


def xml_vm(nom, reseau, disques=("/var/lib/libvirt/images/{nom}.qcow2",), nvram=None, backing=False):
    ds = ""
    for i, d in enumerate(disques):
        ds += (f"<disk type='file' device='disk'><driver name='qemu' type='qcow2'/>"
               f"<source file='{d.format(nom=nom)}'/>"
               + ("<backingStore type='file'><source file='/base.qcow2'/></backingStore>" if backing else "")
               + f"<target dev='vd{chr(97 + i)}' bus='virtio'/></disk>")
    nv = f"<nvram>{nvram}</nvram>" if nvram else ""
    return (f"<domain type='kvm'><name>{nom}</name><uuid>bbbb</uuid><os><type>hvm</type>{nv}</os>"
            f"<devices>{ds}<interface type='network'><source network='{reseau}'/></interface></devices></domain>")


def machine_avec_labo(nom, reseau="lab1", vms=("vm1", "vm2"), actives=(), **kw):
    """FauxHote portant un réseau et des VM branchées dessus."""
    h = FauxHote(nom, **kw)
    h.etat["reseaux"][reseau] = xml_reseau(reseau)
    for v in vms:
        h.etat["domaines"][v] = xml_vm(v, reseau)
    h.etat["actifs"] = set(actives)
    return h
