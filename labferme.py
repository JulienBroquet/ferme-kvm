#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
labferme.py — Ferme KVM 1.0.0 — Sauvegarde et déploiement de labos KVM/libvirt sur une ferme de serveurs Debian.

Un « labo » = un ou plusieurs réseaux virtuels libvirt + toutes les VM qui y sont branchées.

Organisation sur le serveur de stockage :
    <chemin>/<labo>/labo.json            manifeste (propriétaire, réseaux, VM, disques)
    <chemin>/<labo>/xml/reseau-*.xml     définitions des réseaux virtuels
    <chemin>/<labo>/xml/vm-*.xml         définitions des VM
    <chemin>/<labo>/disques/*            disques (qcow2/raw) et NVRAM UEFI

Commandes :
    preparer     Clés SSH, droits sudo, paquets (une fois, puis à chaque nouveau serveur)
    lister       Labos disponibles sur le serveur de stockage
    inventaire   Réseaux virtuels et VM présents sur les serveurs de la ferme
    sauvegarder  Sauvegarde un labo (de ce poste ou d'un serveur) vers le stockage
    deployer     Déploie un labo sur toute la ferme, un pool ou des serveurs précis
    retirer      Supprime un labo (VM, disques, réseaux) des serveurs choisis
    isos         Liste les images ISO du stockage
    iso-envoyer  Copie une ISO de ce poste vers le stockage
    iso-copier   Copie une ISO du stockage vers des serveurs et/ou ce poste
    iso-supprimer Supprime une ISO du stockage

Dépendances du poste maître :  apt install python3-paramiko python3-yaml rsync
"""

import argparse
import getpass
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

try:
    import yaml
    import paramiko
except ImportError:
    sys.exit("Modules manquants : sudo apt install python3-paramiko python3-yaml")

__version__ = "1.0.0"                      # version de Ferme KVM (moteur + interface)
DATE_VERSION = "2026-10-03"

VIRSH = "virsh -q -c qemu:///system"
NOM_CLE = "labferme"                       # ~/.ssh/labferme sur chaque machine
IMAGES_DEFAUT = "/var/lib/libvirt/images/labos"
ISOS_DEFAUT = "/var/lib/libvirt/images/iso"      # où les ISO sont copiées sur les PC
POOL_ISO = "labferme-iso"                       # pool libvirt créé sur ce dossier (visible dans virt-manager)
ISOS_LOCAL = ISOS_DEFAUT
SSH_OPTS = "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
SUDO_CMDS = ["/usr/bin/rsync", "/usr/bin/virsh", "/usr/bin/qemu-img", "/usr/bin/mkdir", "/usr/bin/rm"]
NOM_VALIDE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ET.register_namespace("libosinfo", "http://libosinfo.org/xmlns/libvirt/domain/1.0")
q = shlex.quote
_verrou_log = threading.Lock()


class Erreur(Exception):
    pass


class ErreurConnexion(Erreur):
    """Le PC ne répond pas ou refuse la connexion SSH."""

    def __init__(self, msg, joignable=False):
        super().__init__(msg)
        self.joignable = joignable      # vrai : le PC répond mais l'authentification échoue


class ErreurDroits(Erreur):
    """Le compte se connecte mais ne peut pas piloter libvirt."""


# --------------------------------------------------------------------------- journal

DOSSIER_LOG = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "labferme")
FICHIER_LOG = os.path.join(DOSSIER_LOG, "labferme.log")
RAPPEL_LOG = None          # l'interface graphique y branche sa fonction d'affichage : f(ligne, niveau)
_journal = logging.getLogger("labferme")


def init_journal():
    """Ouvre le fichier journal (rotation : 5 × 2 Mo). Renvoie son chemin."""
    global FICHIER_LOG, DOSSIER_LOG
    if _journal.handlers:
        return FICHIER_LOG
    try:
        os.makedirs(DOSSIER_LOG, exist_ok=True)
        gestionnaire = logging.handlers.RotatingFileHandler(FICHIER_LOG, maxBytes=2_000_000, backupCount=5,
                                                            encoding="utf-8")
    except OSError:
        DOSSIER_LOG = tempfile.gettempdir()
        FICHIER_LOG = os.path.join(DOSSIER_LOG, f"labferme-{getpass.getuser()}.log")
        gestionnaire = logging.handlers.RotatingFileHandler(FICHIER_LOG, maxBytes=2_000_000, backupCount=5,
                                                            encoding="utf-8")
    gestionnaire.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    _journal.addHandler(gestionnaire)
    _journal.setLevel(logging.DEBUG)
    _journal.propagate = False
    return FICHIER_LOG


NIVEAUX = {"erreur": logging.ERROR, "alerte": logging.WARNING, "succes": logging.INFO, "info": logging.INFO}


def log(prefixe, msg, niveau=None):
    """Message visible (console ou interface) + fichier journal."""
    if niveau is None:
        if prefixe == "ERREUR" or re.search(r"ÉCHEC|ERREUR|injoignable|hors ligne", msg):
            niveau = "erreur"
        elif re.match(r"(OK|terminé|en ligne)", msg) or "terminée" in msg:
            niveau = "succes"
        else:
            niveau = "info"
    ligne = f"[{prefixe}] {msg}"
    _journal.log(NIVEAUX.get(niveau, logging.INFO), ligne)
    with _verrou_log:
        if RAPPEL_LOG:
            RAPPEL_LOG(ligne, niveau)
        else:
            print(ligne, flush=True)


def detail(prefixe, msg):
    """Détail technique : fichier journal uniquement."""
    _journal.debug(f"[{prefixe}] {msg}")


def propre(nom):
    """Nom utilisable dans un chemin de fichier."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", nom)


def lignes(texte):
    return [l.strip() for l in texte.splitlines() if l.strip()]


# --------------------------------------------------------------------------- hôtes

class Hote:
    """Une machine joignable en SSH (ou le poste local)."""

    def __init__(self, nom, ip, utilisateur, mot_de_passe=None, pools=(), images=IMAGES_DEFAUT, local=False,
                 mdp_root=None):
        self.nom, self.ip, self.utilisateur = nom, ip, utilisateur
        self.mot_de_passe, self.pools, self.images, self.local = mot_de_passe, list(pools), images, local
        self.mdp_root = mdp_root      # facultatif : sert uniquement à « preparer » si le compte n'est pas sudoer
        self.chemin = None            # utilisé pour le stockage
        self.chemin_iso = None        # stockage : dossier des ISO
        self.isos = ISOS_DEFAUT       # PC : dossier où copier les ISO
        self._client, self._home, self._mode = None, None, None
        self._verrou = threading.Lock()

    def clone(self):
        """Copie indépendante (sa propre connexion SSH), pour les tests en parallèle des opérations."""
        c = Hote(self.nom, self.ip, self.utilisateur, self.mot_de_passe, self.pools, self.images,
                 self.local, self.mdp_root)
        c.chemin, c.chemin_iso, c.isos = self.chemin, self.chemin_iso, self.isos
        return c

    @property
    def root(self):
        return self.utilisateur == "root"

    # Mode de privilège pour libvirt : "root", "sudo" (règle sudoers) ou "groupe" (membre du groupe libvirt)
    @property
    def mode(self):
        if self._mode is None:
            self._mode = self.detecter_mode()
            detail(self.nom, f"mode de privilège : {self._mode}")
        return self._mode

    def detecter_mode(self):
        if self.root:
            return "root"
        if self.run("sudo -n /usr/bin/virsh --version", verifier=False)[0] == 0:
            return "sudo"
        if self.run(f"{VIRSH} list --all --name", verifier=False)[0] == 0:
            return "groupe"
        groupes = self.run("id -nG", verifier=False)[1].split()
        sudoer = "sudo" in groupes or "wheel" in groupes
        raise ErreurDroits(
            f"{self.nom} : le compte « {self.utilisateur} » ne peut pas piloter libvirt "
            f"({'sudoer mais sudo demande un mot de passe' if sudoer else 'pas sudoer'}, "
            f"{'membre' if 'libvirt' in groupes else 'pas membre'} du groupe libvirt). "
            f"Solution : « Préparer la ferme » sur ce PC"
            f"{'' if sudoer else ' avec le mot de passe root (demandé par l’interface ou mot_de_passe_root dans ferme.yaml)'}.")

    def sudo(self, cmd):
        return f"sudo -n {cmd}" if self.mode == "sudo" else cmd

    def _connecter(self):
        with self._verrou:
            if self._client is None:
                c = paramiko.SSHClient()
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cle = os.path.expanduser(f"~/.ssh/{NOM_CLE}")
                try:
                    c.connect(self.ip, username=self.utilisateur, password=self.mot_de_passe,
                              key_filename=cle if os.path.exists(cle) else None,
                              timeout=10, banner_timeout=15, auth_timeout=15)
                except paramiko.AuthenticationException as e:
                    raise ErreurConnexion(f"{self.nom} ({self.ip}) : authentification SSH refusée pour "
                                          f"« {self.utilisateur} » ({e}). Vérifiez le mot de passe dans "
                                          f"ferme.yaml puis « Préparer la ferme ».", joignable=True)
                except Exception as e:
                    raise ErreurConnexion(f"{self.nom} ({self.ip}) : connexion SSH impossible — {e}")
                self._client = c
            return self._client

    def run(self, cmd, entree=None, verifier=True):
        detail(self.nom, f"$ {cmd[:500]}")
        if self.local:
            p = subprocess.run(["bash", "-c", cmd], input=entree, capture_output=True, text=True)
            rc, out, err = p.returncode, p.stdout, p.stderr
        else:
            stdin, stdout, stderr = self._connecter().exec_command(cmd)
            if entree is not None:
                stdin.write(entree)
                stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            rc = stdout.channel.recv_exit_status()
        if rc != 0:
            detail(self.nom, f"  → code {rc} : {(err or out).strip()[:1500]}")
        if verifier and rc != 0:
            texte = (err or out).strip()
            conseil = conseil_erreur(cmd, texte)
            raise Erreur(f"{self.nom} : échec de « {cmd[:150]} » (code {rc}) : {texte}"
                         + (f"\n\n→ {conseil}" if conseil else ""))
        return rc, out, err

    def sortie(self, cmd, **kw):
        return self.run(cmd, **kw)[1]

    def virsh(self, args, **kw):
        # LC_ALL=C : sorties de virsh en anglais, donc analysables quelle que soit la langue du PC
        return self.run("LC_ALL=C " + self.sudo(f"{VIRSH} {args}"), **kw)

    def ecrire(self, chemin, contenu):
        self.run(f"cat > {q(chemin)}", entree=contenu)

    @property
    def home(self):
        if self._home is None:
            self._home = os.path.expanduser("~") if self.local else self.sortie("echo $HOME").strip()
        return self._home

    @property
    def cle(self):
        return f"{self.home}/.ssh/{NOM_CLE}"

    def run_su(self, script, mdp_root, delai=900):
        """Exécute un script en root via « su » (compte non sudoer). Renvoie (code, sortie)."""
        cmd = f"LC_ALL=C su - root -c {q('bash -c ' + q(script))}"
        detail(self.nom, "$ su - root -c <script de préparation>")
        if self.local:
            return _su_local(cmd, mdp_root, delai)
        canal = self._connecter().get_transport().open_session()
        canal.get_pty(width=200)
        canal.settimeout(delai)
        canal.exec_command(cmd)
        try:
            sortie = _dialogue_su(canal.recv, canal.sendall, mdp_root)
        except socket.timeout:
            raise Erreur(f"{self.nom} : délai dépassé pendant « su »")
        return canal.recv_exit_status(), sortie

    def fermer(self):
        if self._client:
            self._client.close()
            self._client = None


def conseil_erreur(cmd, texte):
    """Explication en clair des erreurs SSH / rsync les plus courantes (NAS Synology compris)."""
    if "Permission denied, please try again" in texte and "rsync" in cmd:
        return ("Le NAS refuse rsync pour ce compte (erreur typique de Synology DSM). Dans DSM : "
                "Panneau de configuration → Services de fichiers → onglet rsync → cocher « Activer le service "
                "rsync » ; puis Utilisateur et groupe → votre compte → Modifier → onglet Applications → "
                "autoriser « rsync ». Testez ensuite : clic droit → « Tester l'accès au stockage ».")
    if "Permission denied (publickey" in texte:
        return ("La clé SSH est refusée par le serveur. Relancez « Préparer » ; si cela persiste, sur le "
                "serveur : chmod 755 ~ ; chmod 700 ~/.ssh ; chmod 600 ~/.ssh/authorized_keys.")
    if "Host key verification failed" in texte or "REMOTE HOST IDENTIFICATION HAS CHANGED" in texte:
        return ("L'empreinte SSH du serveur a changé (réinstallation ?). Effacez l'ancienne : "
                "ssh-keygen -R <ip> et sudo ssh-keygen -R <ip>.")
    if re.search(r"Connection refused|timed out|No route to host|Connection closed by", texte):
        return ("Le serveur ne répond pas en SSH ou bloque ce PC (Synology : Panneau de configuration → "
                "Sécurité → Protection → Blocage automatique, vérifiez la liste des IP bloquées).")
    if re.search(r"rsync: (command )?not found|rsync: introuvable", texte):
        return "rsync n'est pas installé (ou pas activé) sur le serveur distant."
    return ""


def _dialogue_su(lire, ecrire, mdp):
    """Attend l'invite de mot de passe de su, l'envoie, puis lit toute la sortie."""
    tampon, envoye = b"", False
    while True:
        d = lire(4096)
        if not d:
            break
        tampon += d
        if not envoye and re.search(rb"(assword|mot de passe)[^\n]*:\s*$", tampon, re.I):
            ecrire((mdp + "\n").encode())
            envoye = True
    return tampon.decode(errors="replace")


def _su_local(cmd, mdp, delai):
    import fcntl
    import pty
    import termios
    maitre, esclave = pty.openpty()

    def terminal():
        os.setsid()
        fcntl.ioctl(esclave, termios.TIOCSCTTY, 0)

    p = subprocess.Popen(["bash", "-c", cmd], stdin=esclave, stdout=esclave, stderr=esclave,
                         preexec_fn=terminal, close_fds=True)
    os.close(esclave)

    def lire(n):
        if not select.select([maitre], [], [], delai)[0]:
            p.kill()
            raise Erreur("délai dépassé pendant « su »")
        try:
            return os.read(maitre, n)
        except OSError:
            return b""

    try:
        sortie = _dialogue_su(lire, lambda b: os.write(maitre, b), mdp)
    finally:
        os.close(maitre)
    return p.wait(), sortie


def hote_local():
    h = Hote("local", "127.0.0.1", getpass.getuser(), local=True)
    h.isos = ISOS_LOCAL
    return h


def charger_config(chemin):
    if not os.path.exists(chemin):
        raise Erreur(f"Fichier de configuration introuvable : {chemin} (option -c)")
    if os.stat(chemin).st_mode & 0o077:
        print(f"Attention : {chemin} contient des mots de passe, faites « chmod 600 {chemin} ».", file=sys.stderr)
    with open(chemin, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    s = cfg["stockage"]
    stockage = Hote("stockage", s["ip"], s.get("utilisateur", "root"), s.get("mot_de_passe"),
                    mdp_root=s.get("mot_de_passe_root"))
    stockage.chemin = s.get("chemin", "/srv/labos").rstrip("/")
    stockage.chemin_iso = (s.get("chemin_iso") or stockage.chemin + "/iso").rstrip("/")
    d = cfg.get("defaut", {}) or {}
    global ISOS_LOCAL
    ISOS_LOCAL = d.get("isos", ISOS_DEFAUT)
    hotes = []
    for h in cfg.get("hotes", []) or []:
        x = Hote(h["nom"], h["ip"], h.get("utilisateur", d.get("utilisateur")),
                 h.get("mot_de_passe", d.get("mot_de_passe")), h.get("pools", []),
                 h.get("images", d.get("images", IMAGES_DEFAUT)),
                 mdp_root=h.get("mot_de_passe_root", d.get("mot_de_passe_root")))
        x.isos = h.get("isos", d.get("isos", ISOS_DEFAUT))
        hotes.append(x)
    return stockage, hotes


def choisir_hotes(hotes, args, tous_par_defaut=False):
    if getattr(args, "tous", False) or (tous_par_defaut and not args.pool and not args.hote):
        return list(hotes)
    noms, pools = set(args.hote or []), set(args.pool or [])
    choix = [h for h in hotes if h.nom in noms or h.ip in noms or pools & set(h.pools)]
    inconnus = noms - {h.nom for h in hotes} - {h.ip for h in hotes}
    if inconnus:
        raise Erreur(f"Serveur(s) inconnu(s) dans la configuration : {', '.join(sorted(inconnus))}")
    if not choix:
        raise Erreur("Aucun serveur sélectionné : utilisez --tous, --pool NOM ou --hote NOM.")
    return choix


def en_parallele(hotes, fonction, nb):
    """Exécute fonction(h) sur chaque hôte ; renvoie (réussis, échoués)."""
    ok, ko = [], []
    with ThreadPoolExecutor(max_workers=max(1, nb)) as ex:
        futurs = {ex.submit(fonction, h): h for h in hotes}
        for f in as_completed(futurs):
            h = futurs[f]
            try:
                f.result()
                ok.append(h.nom)
            except Exception as e:
                log(h.nom, f"ÉCHEC : {e}")
                detail(h.nom, traceback.format_exc())
                ko.append(h.nom)
            finally:
                h.fermer()
    return ok, ko


# --------------------------------------------------------------------------- libvirt : lecture

def reseau_ip(ip_el):
    pref = ip_el.get("prefix") or ip_el.get("netmask") or "24"
    return ipaddress.ip_interface(f"{ip_el.get('address')}/{pref}").network


def ips_v4(racine):
    return [ip for ip in racine.findall("ip") if ip.get("family", "ipv4") == "ipv4" and ip.get("address")]


def infos_reseau(xml):
    r = ET.fromstring(xml)
    br = r.find("bridge")
    fwd = r.find("forward")
    return {"xml": xml,
            "bridge": br.get("name") if br is not None else None,
            "mode": (fwd.get("mode", "nat") if fwd is not None else "isolé"),
            "subnets": [reseau_ip(ip) for ip in ips_v4(r)]}


def lire_reseaux(h):
    actifs = set(lignes(h.virsh("net-list --name")[1]))
    res = {}
    for n in lignes(h.virsh("net-list --all --name")[1]):
        res[n] = infos_reseau(h.virsh(f"net-dumpxml {q(n)}")[1])
        res[n]["actif"] = n in actifs
    return res


def interfaces_hote(h):
    """Noms d'interfaces existantes + sous-réseaux IPv4 déjà utilisés par l'hôte."""
    liens = set()
    for l in h.sortie("ip -o link show").splitlines():
        parts = l.split(":")
        if len(parts) > 1:
            liens.add(parts[1].strip().split("@")[0])
    nets = []
    for l in h.sortie("ip -o -4 addr show").splitlines():
        m = re.search(r"inet (\S+)", l)
        if m:
            nets.append(ipaddress.ip_interface(m.group(1)).network)
    return liens, nets


def lire_domaines(h):
    tous = lignes(h.virsh("list --all --name")[1])
    actifs = set(lignes(h.virsh("list --name")[1]))
    return tous, actifs


def branchements(xml):
    """Réseaux libvirt et ponts auxquels une VM est reliée."""
    nets, ponts = set(), set()
    for i in ET.fromstring(xml).findall("devices/interface"):
        s = i.find("source")
        if s is None:
            continue
        if i.get("type") == "network":
            nets.add(s.get("network"))
        elif i.get("type") == "bridge":
            ponts.add(s.get("bridge"))
    return nets, ponts


def disques(h, xml):
    out = []
    for d in ET.fromstring(xml).findall("devices/disk"):
        s, t = d.find("source"), d.find("target")
        chemin = None
        if s is not None:
            if d.get("type") == "volume" and s.get("pool"):
                chemin = h.virsh(f"vol-path --pool {q(s.get('pool'))} {q(s.get('volume'))}")[1].strip()
            else:
                chemin = s.get("file")
        out.append({"device": d.get("device", "disk"), "type": d.get("type"),
                    "source": chemin, "cible": t.get("dev") if t is not None else None})
    return out


def arreter_vm(h, nom, delai=120):
    log(h.nom, f"arrêt propre de « {nom} »…")
    h.virsh(f"shutdown {q(nom)}", verifier=False)
    fin = time.time() + delai
    while time.time() < fin:
        if nom not in lire_domaines(h)[1]:
            return
        time.sleep(3)
    log(h.nom, f"« {nom} » ne s'arrête pas, arrêt forcé")
    h.virsh(f"destroy {q(nom)}", verifier=False)


def supprimer_vm(h, nom):
    h.virsh(f"destroy {q(nom)}", verifier=False)
    if h.virsh(f"undefine --nvram {q(nom)}", verifier=False)[0] != 0:
        h.virsh(f"undefine {q(nom)}")


def definir(h, genre, xml):
    """genre = 'net' ou 'dom' ; écrit le XML dans un fichier temporaire puis le définit."""
    tmp = h.sortie("mktemp /tmp/labferme-XXXXXX.xml").strip()
    try:
        h.ecrire(tmp, xml)
        h.virsh(f"{'net-define' if genre == 'net' else 'define'} {q(tmp)}")
    finally:
        h.run(f"rm -f {q(tmp)}", verifier=False)


# --------------------------------------------------------------------------- réseau : adaptation

def pont_libre(pris):
    n = 1
    while f"virbr{n}" in pris:
        n += 1
    return f"virbr{n}"


def sous_reseau_libre(net, pris):
    """Décale le sous-réseau par pas de sa propre taille jusqu'à en trouver un libre (privé)."""
    for i in range(1, 4096):
        base = int(net.network_address) + i * net.num_addresses
        try:
            cand = ipaddress.ip_network((base, net.prefixlen))
        except ValueError:
            break
        if cand.is_private and not any(cand.overlaps(p) for p in pris):
            return cand
    raise Erreur(f"aucun sous-réseau libre trouvé à partir de {net}")


def decaler_ip(ip_el, ancien, nouveau):
    delta = int(nouveau.network_address) - int(ancien.network_address)
    dec = lambda a: str(ipaddress.ip_address(a) + delta)
    ip_el.set("address", dec(ip_el.get("address")))
    for rg in ip_el.iter("range"):
        rg.set("start", dec(rg.get("start")))
        rg.set("end", dec(rg.get("end")))
    for hst in ip_el.iter("host"):
        if hst.get("ip"):
            hst.set("ip", dec(hst.get("ip")))


def nettoyer_reseau(xml):
    r = ET.fromstring(xml)
    for tag in ("uuid", "mac"):
        e = r.find(tag)
        if e is not None:
            r.remove(e)
    return ET.tostring(r, encoding="unicode")


def adapter_reseau(xml, ponts_pris, subnets_pris, conflit_ip, p):
    """Rend un réseau compatible avec la cible : pont libre (virbrN) et sous-réseau sans chevauchement."""
    r = ET.fromstring(xml)
    nom = r.findtext("name")
    br = r.find("bridge")
    if br is None:
        br = ET.SubElement(r, "bridge")
    ancien = br.get("name")
    pont = ancien if ancien and ancien not in ponts_pris else pont_libre(ponts_pris)
    if pont != ancien:
        log(p, f"réseau « {nom} » : pont {ancien or '(aucun)'} déjà pris → {pont}")
    br.set("name", pont)
    br.set("stp", br.get("stp", "on"))
    br.set("delay", br.get("delay", "0"))
    ponts_pris.add(pont)
    for ip in ips_v4(r):
        net = reseau_ip(ip)
        if any(net.overlaps(s) for s in subnets_pris):
            if conflit_ip == "decaler":
                nouveau = sous_reseau_libre(net, subnets_pris)
                decaler_ip(ip, net, nouveau)
                log(p, f"réseau « {nom} » : {net} déjà utilisé → décalé en {nouveau} "
                       f"(les IP fixes configurées DANS les VM devront être adaptées)")
                net = nouveau
            elif conflit_ip == "sans-ip":
                r.remove(ip)
                log(p, f"réseau « {nom} » : {net} déjà utilisé → réseau créé sans IP côté hôte (pas de DHCP)")
                continue
            else:
                raise Erreur(f"réseau « {nom} » : le sous-réseau {net} chevauche un réseau existant. "
                             f"Relancez avec --conflit-ip decaler ou --conflit-ip sans-ip.")
        subnets_pris.append(net)
    return ET.tostring(r, encoding="unicode"), pont


def adapter_vm(xml, chemins, formats, nvram, ponts, h, p):
    """Réécrit le XML d'une VM pour la cible : chemins des disques, NVRAM, ponts → réseaux libvirt."""
    r = ET.fromstring(xml)
    u = r.find("uuid")
    if u is not None:
        r.remove(u)
    for d in r.findall("devices/disk"):
        t, s = d.find("target"), d.find("source")
        dev = t.get("dev") if t is not None else None
        if d.get("device", "disk") == "disk" and dev in chemins:
            d.set("type", "file")
            if s is None:
                s = ET.SubElement(d, "source")
            s.attrib.clear()
            s.set("file", chemins[dev])
            # Le disque copié est autonome (aplati à l'export) : on retire le lien vers l'ancienne image de base,
            # sinon libvirt la cherche sur le PC cible (« Impossible d'accéder au fichier de stockage »).
            for b in d.findall("backingStore"):
                d.remove(b)
            drv = d.find("driver")
            if drv is not None and formats.get(dev):
                drv.set("type", formats[dev])
        elif d.get("device") in ("cdrom", "floppy") and s is not None and s.get("file"):
            if h.run(f"test -e {q(s.get('file'))}", verifier=False)[0] != 0:
                d.remove(s)
                log(p, f"{r.findtext('name')} : image {s.get('file')} absente → lecteur vidé")
    for i in r.findall("devices/interface"):
        s = i.find("source")
        if s is not None and i.get("type") == "bridge" and s.get("bridge") in ponts:
            reseau = ponts[s.get("bridge")]
            i.set("type", "network")
            s.attrib.clear()
            s.set("network", reseau)
    osel, nv = r.find("os"), r.find("os/nvram")
    if nv is not None:
        if nvram:
            nv.text = nvram
        else:                         # libvirt recréera une NVRAM vierge à partir du modèle
            osel.remove(nv)
    return ET.tostring(r, encoding="unicode")


# --------------------------------------------------------------------------- commande : preparer

def executer_en_root(h, script, demander_root=None):
    """Exécute un script bash en root :
    1. directement si le compte est root ;
    2. via sudo avec le mot de passe du compte, s'il est sudoer ;
    3. sinon via su avec le mot de passe root (ferme.yaml : mot_de_passe_root, ou demander_root(h))."""
    if h.root:
        return h.run(f"bash -c {q(script)}")
    if h.local and not h.mot_de_passe and not h.mdp_root and demander_root is None:
        print("Configuration du poste local : sudo va demander votre mot de passe.")
        if subprocess.run(["sudo", "bash", "-c", script]).returncode != 0:
            raise Erreur("configuration sudo locale échouée")
        return
    raison = "aucun mot de passe de compte"
    if h.mot_de_passe:
        rc, out, err = h.run("LC_ALL=C sudo -S -p '' true", entree=h.mot_de_passe + "\n", verifier=False)
        if rc == 0:
            log(h.nom, "droits root obtenus via sudo")
            return h.run(f"LC_ALL=C sudo -S -p '' bash -c {q(script)}", entree=h.mot_de_passe + "\n")
        raison = (err or out).strip().splitlines()[-1] if (err or out).strip() else f"code {rc}"
        log(h.nom, f"sudo impossible pour « {h.utilisateur} » ({raison}) → tentative avec su (root)", "alerte")
    mdp = h.mdp_root or (demander_root(h) if demander_root else None)
    if not mdp:
        raise Erreur(f"{h.nom} : le compte « {h.utilisateur} » ne peut pas utiliser sudo ({raison}). "
                     f"Il faut le mot de passe root de ce PC (mot_de_passe_root dans ferme.yaml, "
                     f"ou saisi dans l'interface).")
    rc, sortie = h.run_su(script, mdp)
    detail(h.nom, f"sortie de su (code {rc}) :\n{sortie[-3000:]}")
    if rc != 0:
        if re.search(r"Authentication failure|authentification|incorrect", sortie, re.I):
            raise Erreur(f"{h.nom} : mot de passe root refusé par su")
        raise Erreur(f"{h.nom} : préparation en root échouée (code {rc}) : {sortie.strip()[-400:]}")
    h.mdp_root = mdp
    log(h.nom, "droits root obtenus via su")


sudo_avec_mdp = executer_en_root          # ancien nom


def autoriser_cles(h, pubs):
    # chmod go-w ~ : sshd refuse les clés si le dossier personnel est modifiable par d'autres (fréquent sur Synology)
    h.run("chmod go-w ~ 2>/dev/null; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys "
          "&& chmod 600 ~/.ssh/authorized_keys")
    existant = h.sortie("cat ~/.ssh/authorized_keys")
    nouvelles = [k for k in pubs if k and k not in existant]
    if nouvelles:
        prefixe = "\n" if existant and not existant.endswith("\n") else ""
        h.run("cat >> ~/.ssh/authorized_keys", entree=prefixe + "\n".join(nouvelles) + "\n")


def nom_cle(h):
    """Commentaire de la clé (visible dans authorized_keys) : labferme-<pc> ou labferme-poste-<compte>-<machine>."""
    if h.local:
        return f"labferme-poste-{propre(getpass.getuser())}-{propre(socket.gethostname())}"
    return f"labferme-{propre(h.nom)}"


def creer_cle(h):
    h.run(f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && (test -f {q(h.cle)} || "
          f"ssh-keygen -q -t ed25519 -N '' -C {q(nom_cle(h))} -f {q(h.cle)})")
    return h.sortie(f"cat {q(h.cle)}.pub").strip()


def script_preparation(h, est_stockage, chemin=None):
    paquets = "rsync" if est_stockage else "rsync qemu-utils sudo"
    s = ["set -e",
         f"if command -v dpkg >/dev/null; then for p in {paquets}; do dpkg -s $p >/dev/null 2>&1 || MANQUE=\"$MANQUE $p\"; done; fi",
         "if [ -n \"$MANQUE\" ]; then apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $MANQUE; fi",
         "command -v rsync >/dev/null || { echo 'rsync introuvable : installez-le (Synology : activer le service rsync)'; exit 1; }"]
    if est_stockage:
        s += [f"mkdir -p {q(chemin)}", f"chown {q(h.utilisateur)}: {q(chemin)}"]
    elif not h.root:
        regle = f"{h.utilisateur} ALL=(root) NOPASSWD: {', '.join(SUDO_CMDS)}"
        f = f"/etc/sudoers.d/labferme-{re.sub(r'[^A-Za-z0-9_-]', '_', h.utilisateur)}"   # un fichier par compte
        s += [f"for g in libvirt kvm; do getent group $g >/dev/null && usermod -aG $g {q(h.utilisateur)} || true; done",
              f"mkdir -p {q(h.images)} {q(h.isos)}",
              "mkdir -p /etc/sudoers.d",
              f"printf '%s\\n' {q(regle)} > {f}.tmp",
              f"chmod 440 {f}.tmp",
              f"visudo -cf {f}.tmp >/dev/null",
              f"mv {f}.tmp {f}"]
    return "\n".join(s)


def cmd_preparer(args, stockage, hotes):
    demander_root = getattr(args, "demander_root", None)
    local = hote_local()
    local.mot_de_passe = getattr(args, "mdp_local", None)
    local.mdp_root = getattr(args, "mdp_local", None)
    pub_maitre = creer_cle(local)
    cibles = choisir_hotes(hotes, args, tous_par_defaut=True)
    pubs = {}
    for h in [stockage] + cibles:
        log(h.nom, "préparation…")
        try:
            autoriser_cles(h, [pub_maitre])
            pubs[h.nom] = creer_cle(h)
            if h is stockage:
                # Stockage (ex. NAS Synology) : pas besoin de root si rsync existe et que le dossier est à nous
                deja_pret = h.run(f"mkdir -p {q(h.chemin)} && test -w {q(h.chemin)} && command -v rsync",
                                  verifier=False)[0] == 0
            else:
                deja_pret = droits_ok(h) and h.mode in ("root", "sudo") and \
                    h.run("command -v rsync && command -v qemu-img", verifier=False)[0] == 0
            if deja_pret:
                log(h.nom, "déjà prêt (rsync et droits en place) → étape root inutile")
            else:
                executer_en_root(h, script_preparation(h, h is stockage, stockage.chemin), demander_root)
            h._mode = None
            if h is not stockage:
                h.fermer()                      # nouvelle session : prise en compte du groupe libvirt
                log(h.nom, f"OK – droits libvirt : {h.mode}")
            else:
                log(h.nom, "OK")
        except Exception as e:
            log(h.nom, f"ÉCHEC : {e}")
            detail(h.nom, traceback.format_exc())
    if "stockage" not in pubs:
        raise Erreur("le serveur de stockage n'a pas pu être préparé")
    autoriser_cles(stockage, [pub_maitre] + [pubs[h.nom] for h in cibles if h.nom in pubs])
    for h in cibles:
        if h.nom in pubs:
            autoriser_cles(h, [pubs["stockage"]])
    if args.local:
        executer_en_root(local, script_preparation(local, False), demander_root)
        log("local", "OK (sudo sans mot de passe pour virsh/rsync/qemu-img)")
    log("maître", "préparation terminée. Les mots de passe ne sont plus nécessaires pour les autres commandes.")


# --------------------------------------------------------------------------- commande : lister / inventaire

def lire_manifeste(stockage, labo):
    if not NOM_VALIDE.match(labo):
        raise Erreur(f"nom de labo invalide : {labo}")
    rc, out, _ = stockage.run(f"cat {q(stockage.chemin)}/{q(labo)}/labo.json", verifier=False)
    if rc != 0:
        raise Erreur(f"labo « {labo} » introuvable sur le stockage (voir la commande « lister »)")
    return json.loads(out)


def tailles_fichiers(stockage, man):
    """Taille (octets) de chaque fichier de disque / NVRAM du labo sur le stockage."""
    base = f"{stockage.chemin}/{man['nom']}"
    fichiers = [d["fichier"] for v in man.get("vms", []) for d in v.get("disques", [])] + \
               [v["nvram"] for v in man.get("vms", []) if v.get("nvram")]
    if not fichiers:
        return {}
    out = stockage.sortie(f"cd {q(base)} && stat -c '%s|%n' " + " ".join(q(f) for f in fichiers), verifier=False)
    tailles = {}
    for l in lignes(out):
        t, _, f = l.partition("|")
        if t.isdigit():
            tailles[f] = int(t)
    return tailles


def besoin_vms(vms, tailles):
    return sum(tailles.get(d["fichier"], 0) for v in vms for d in v["disques"]) + \
        sum(tailles.get(v["nvram"], 0) for v in vms if v.get("nvram"))


def lister_labos(stockage):
    """Manifestes des labos présents sur le stockage, avec leur taille (clé « taille »)."""
    noms = lignes(stockage.sortie(f"cd {q(stockage.chemin)} && for d in */; do "
                                  f"[ -f \"$d/labo.json\" ] && echo \"${{d%/}}\"; done; true"))
    labos = []
    for n in sorted(noms):
        try:
            m = lire_manifeste(stockage, n)
        except (Erreur, ValueError) as e:
            log("stockage", f"labo « {n} » illisible : {e}")
            continue
        m["nom"] = n
        m["octets"] = sum(tailles_fichiers(stockage, m).values())
        m["taille"] = taille_lisible(m["octets"])
        labos.append(m)
    return labos


def labos_locaux(h):
    """Réseaux virtuels d'une machine avec les VM branchées dessus (candidats à l'export)."""
    reseaux = lire_reseaux(h)
    tous, actifs = lire_domaines(h)
    par_reseau = {n: [] for n in reseaux}
    ponts = {i["bridge"]: n for n, i in reseaux.items() if i["bridge"]}
    for d in tous:
        nets, brs = branchements(h.virsh(f"dumpxml --inactive {q(d)}")[1])
        for n in nets | {ponts[b] for b in brs if b in ponts}:
            if n in par_reseau and d not in par_reseau[n]:
                par_reseau[n].append(d)
    return [{"nom": n, "bridge": i["bridge"], "mode": i["mode"], "actif": i["actif"],
             "subnets": [str(s) for s in i["subnets"]],
             "vms": [(d, d in actifs) for d in par_reseau[n]]}
            for n, i in sorted(reseaux.items())]


MARGE_DISQUE = 2 * 1024 ** 3       # place gardée libre en plus du labo (2 Go)
ESPACES = {}                         # nom du PC -> octets libres dans son dossier d'images (dernier test)


def espace_libre(h, chemin):
    """Octets libres sur le disque qui contient chemin (ou son plus proche parent existant)."""
    out = h.sortie(f"d={q(chemin)}; while [ ! -d \"$d\" ]; do d=$(dirname \"$d\"); done; "
                   f"df -P -k \"$d\" | tail -1", verifier=False).split()
    try:
        return int(out[3]) * 1024
    except (IndexError, ValueError):
        return None


def verifier_place(h, besoin, quoi):
    """Erreur si le dossier d'images de h n'a pas assez de place pour « besoin » octets (+ marge)."""
    libre = espace_libre(h, h.images if quoi != "iso" else h.isos)
    if libre is None:
        log(h.nom, "espace libre inconnu → vérification ignorée", "alerte")
        return
    ESPACES[h.nom] = libre
    if libre < besoin + MARGE_DISQUE:
        raise Erreur(f"place insuffisante : il faut {taille_lisible(besoin)} (+ {taille_lisible(MARGE_DISQUE)} "
                     f"de marge), il reste {taille_lisible(libre)} libres")
    detail(h.nom, f"place OK : besoin {taille_lisible(besoin)}, libre {taille_lisible(libre)}")


CHARGES = {}                         # nom du PC -> dernière mesure de charge (cpu %, ram %…)
HISTORIQUE = {}                      # nom du PC -> deque de (horodatage, cpu %, ram %)
SEUIL_ATTENTION, SEUIL_CRITIQUE = 75, 90


def charge_hote(h):
    """CPU (mesuré sur 1 s), mémoire et charge moyenne d'un PC. Aucun droit particulier nécessaire."""
    out = h.sortie("head -1 /proc/stat; sleep 1; head -1 /proc/stat; "
                   "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; nproc; cat /proc/loadavg")
    l = out.splitlines()
    a, b = [list(map(int, x.split()[1:9])) for x in l[:2]]
    total = sum(b) - sum(a)
    repos = (b[3] + b[4]) - (a[3] + a[4])
    cpu = 100.0 * (1 - repos / total) if total > 0 else 0.0
    mem = {x.split(":")[0]: int(x.split()[1]) * 1024 for x in l[2:4]}
    ram_tot, ram_dispo = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    ram = 100.0 * (ram_tot - ram_dispo) / ram_tot if ram_tot else 0.0
    m = {"cpu": max(0.0, min(100.0, cpu)), "ram": ram, "ram_utilisee": ram_tot - ram_dispo, "ram_totale": ram_tot,
         "coeurs": int(l[4]), "charge1": float(l[5].split()[0]), "heure": time.time()}
    CHARGES[h.nom] = m
    HISTORIQUE.setdefault(h.nom, deque(maxlen=600)).append((m["heure"], m["cpu"], m["ram"]))
    return m


def niveau_charge(m):
    """vert / orange / rouge selon le plus chargé du CPU et de la RAM."""
    pic = max(m["cpu"], m["ram"])
    return "rouge" if pic >= SEUIL_CRITIQUE else "orange" if pic >= SEUIL_ATTENTION else "vert"


def _mesurer(c):
    try:
        charge_hote(c)
    except Exception as e:
        detail(c.nom, f"mesure de charge impossible : {e}")


LIBELLES_MODE = {"root": "compte root", "sudo": "sudo (règle labferme)", "groupe": "groupe libvirt"}


def sonder(h, stockage=False, delai=4):
    """Test d'un PC sans perturber les opérations en cours.
    Renvoie (couleur, texte court, détail) ; couleur = vert | orange | rouge."""
    c = h.clone()
    try:
        if c.local:
            return sonder_local(c)
        try:
            socket.create_connection((c.ip, 22), timeout=delai).close()
        except OSError as e:
            return "rouge", "hors ligne", f"{c.nom} ({c.ip}) : le port SSH 22 ne répond pas ({e}). " \
                                          f"PC éteint, débranché ou SSH non installé."
        if stockage:
            if c.run(f"test -d {q(c.chemin)} && test -w {q(c.chemin)}", verifier=False)[0] != 0:
                return "orange", "dossier inaccessible", f"Le dossier {c.chemin} n'existe pas ou n'est pas " \
                                                         f"accessible en écriture pour {c.utilisateur}. " \
                                                         f"Lancez « Préparer la ferme »."
            libre = espace_libre(c, c.chemin)
            ESPACES["stockage"] = libre
            return "vert", f"en ligne – {taille_lisible(libre) if libre is not None else '?'} libres", \
                f"{c.utilisateur}@{c.ip}:{c.chemin}"
        mode = c.mode
        reseaux = lignes(c.virsh("net-list --all --name")[1])
        vms = lignes(c.virsh("list --all --name")[1])
        ESPACES[c.nom] = espace_libre(c, c.images)
        _mesurer(c)
        if mode == "groupe":
            return "orange", "droits partiels", \
                f"{c.nom} : libvirt est accessible (groupe libvirt), mais le compte « {c.utilisateur} » ne peut pas " \
                f"écrire dans {c.images} et {c.isos}. Lancez « Préparer » sur ce PC."
        return "vert", f"en ligne – {len(reseaux)} réseau(x), {len(vms)} VM", \
               f"{c.nom} ({c.ip}) : connexion OK, droits libvirt via {LIBELLES_MODE[mode]}."
    except ErreurConnexion as e:
        return ("orange" if e.joignable else "rouge"), \
               ("SSH refusé" if e.joignable else "hors ligne"), str(e)
    except ErreurDroits as e:
        return "orange", "droits insuffisants", str(e)
    except Exception as e:
        return "orange", "erreur", str(e)
    finally:
        c.fermer()


def tester_acces_stockage(h, stockage):
    """Teste, depuis le PC h, les liaisons réellement utilisées pour les ISO et les sauvegardes.
    Renvoie une liste de (étape, ok, détail)."""
    res = []
    cle_ok = h.run(f"test -f {q(h.cle)}", verifier=False)[0] == 0
    res.append(("clé SSH présente", cle_ok, h.cle if cle_ok else "absente : lancez « Préparer »"))
    if not cle_ok:
        return res
    ssh_e = f"ssh -i {h.cle} {SSH_OPTS} -o PasswordAuthentication=no"
    cible = f"{stockage.utilisateur}@{stockage.ip}"
    rc, out, err = h.run(f"{ssh_e} {cible} echo labferme-ok", verifier=False)
    ok = rc == 0 and "labferme-ok" in out
    res.append(("connexion SSH par clé vers le stockage", ok,
                "OK" if ok else f"{(err or out).strip()[-300:]}\n→ {conseil_erreur('ssh', err or out)}"))
    if not ok:
        return res
    for etiquette, prefixe in (("rsync vers le stockage", ""),
                               ("rsync via sudo (téléchargements dans /var/lib/libvirt)", "sudo -n ")):
        if prefixe and h.mode != "sudo":
            continue
        rc, out, err = h.run(f"{prefixe}rsync --list-only -e {q(ssh_e)} {q(cible + ':' + stockage.chemin_iso + '/')}",
                             verifier=False)
        ok = rc == 0
        res.append((etiquette, ok, "OK" if ok else
                    f"{(err or out).strip()[-300:]}\n→ {conseil_erreur('rsync', err or out)}"))
    return res


def sonder_local(c):
    """État du poste sur lequel tourne le programme (pas de SSH : tests directs)."""
    try:
        mode = c.mode
    except ErreurDroits:
        return "orange", "à préparer", "Ce poste ne peut pas piloter libvirt. Clic droit → « Préparer ce poste… »."
    if mode == "groupe":
        return "orange", "à préparer", \
            "Ce poste accède à libvirt (groupe libvirt) mais ne peut pas écrire dans /var/lib/libvirt/images " \
            "(nécessaire pour les ISO et les exports). Clic droit → « Préparer ce poste… »."
    ESPACES["local"] = espace_libre(c, c.isos)
    _mesurer(c)
    if not os.path.exists(c.cle):
        return "orange", "à préparer", "Clé SSH du poste absente. Clic droit → « Préparer ce poste… »."
    return "vert", "prêt", f"Ce poste ({c.utilisateur}) : droits libvirt via {LIBELLES_MODE[mode]}, clé {c.cle}."


def local_pret():
    """Vrai si ce poste a les droits complets (root ou règle sudo) et sa clé SSH."""
    return sonder_local(hote_local())[0] == "vert"


def preparer_local(stockage, mdp, demander_root=None):
    """Prépare uniquement ce poste : clé SSH autorisée sur le stockage + droits (sudo/groupe/dossiers).
    Aucun autre PC n'est touché."""
    local = hote_local()
    local.mot_de_passe = mdp          # mot de passe du compte (sudo) ; root demandé à part si besoin
    log("local", "préparation de ce poste…")
    pub = creer_cle(local)
    autoriser_cles(stockage, [pub])
    log("local", "clé SSH de ce poste autorisée sur le stockage")
    if sonder_local(local)[0] != "vert":
        executer_en_root(local, script_preparation(local, False), demander_root)
    couleur, texte, det = sonder_local(hote_local())
    log("local", f"{'OK' if couleur == 'vert' else 'incomplet'} – {det}", "succes" if couleur == "vert" else "alerte")
    return couleur


def verifier_hote(h):
    couleur, texte, _ = sonder(h)
    if couleur != "vert":
        raise Erreur(texte)
    return texte


def droits_ok(h):
    try:
        h._mode = None
        h.mode
        return True
    except Erreur:
        return False


def sudo_local_ok():
    return droits_ok(hote_local())


def cmd_lister(args, stockage, hotes):
    labos = lister_labos(stockage)
    if not labos:
        print("Aucun labo sur le serveur de stockage.")
        return
    print(f"{'LABO':<24}{'PROPRIÉTAIRE':<16}{'VM':>4}  {'TAILLE':>8}  {'DATE':<17}RÉSEAUX")
    for m in labos:
        n, taille = m["nom"], m["taille"]
        print(f"{n:<24}{m.get('proprietaire', '?'):<16}{len(m['vms']):>4}  {taille:>8}  "
              f"{m.get('date', '?'):<17}{', '.join(r['nom'] for r in m['reseaux'])}")
        if args.details:
            if m.get("description"):
                print(f"    {m['description']}")
            for v in m["vms"]:
                print(f"    - {v['nom']} ({len(v['disques'])} disque(s))")


def cmd_inventaire(args, stockage, hotes):
    cibles = choisir_hotes(hotes, args, tous_par_defaut=True)
    resultats = {}

    def lire(h):
        reseaux = lire_reseaux(h)
        tous, actifs = lire_domaines(h)
        resultats[h.nom] = (h, reseaux, tous, actifs)

    ok, ko = en_parallele(cibles, lire, args.parallele)
    for nom in sorted(resultats):
        h, reseaux, tous, actifs = resultats[nom]
        print(f"\n== {nom} ({h.ip})  pools : {', '.join(h.pools) or '-'}")
        for n, i in sorted(reseaux.items()):
            print(f"   réseau {n:<22} pont {i['bridge'] or '-':<9} {i['mode']:<7} "
                  f"{', '.join(map(str, i['subnets'])) or '-':<18} {'actif' if i['actif'] else 'inactif'}")
        for d in tous:
            print(f"   vm     {d:<22} {'en marche' if d in actifs else 'arrêtée'}")
    if ko:
        print(f"\nInjoignables : {', '.join(ko)}")


# --------------------------------------------------------------------------- commande : sauvegarder

def sans_backing(xml):
    """Retire les chaînes d'images de base (<backingStore>) : les disques exportés sont aplatis."""
    r = ET.fromstring(xml)
    for d in r.findall("devices/disk"):
        for b in d.findall("backingStore"):
            d.remove(b)
    return ET.tostring(r, encoding="unicode")


def copier_vers(src, chemin, destination, ssh_e):
    """Copie un fichier de src vers destination (user@ip:chemin) ; aplatit les chaînes de snapshots."""
    info = json.loads(src.sortie(src.sudo(f"qemu-img info -U --output=json {q(chemin)}")))
    fmt = info.get("format", "raw")
    a_copier, tmp = chemin, None
    if info.get("backing-filename"):
        tmp = f"/var/tmp/labferme-{os.getpid()}-{propre(os.path.basename(chemin))}.qcow2"
        log(src.nom, f"{os.path.basename(chemin)} a une image de base → aplatissement en qcow2")
        src.run(src.sudo(f"qemu-img convert -U -O qcow2 {q(chemin)} {q(tmp)}"))
        a_copier, fmt = tmp, "qcow2"
    try:
        src.run(src.sudo(f"rsync -a --sparse -e {q(ssh_e)} {q(a_copier)} {q(destination)}"))
    finally:
        if tmp:
            src.run(src.sudo(f"rm -f {q(tmp)}"), verifier=False)
    return fmt


def cmd_sauvegarder(args, stockage, hotes):
    if args.hote:
        trouve = [h for h in hotes if args.hote in (h.nom, h.ip)]
        if not trouve:
            raise Erreur(f"serveur « {args.hote} » absent de la configuration")
        src = trouve[0]
    else:
        src = hote_local()
    p = src.nom
    reseaux = lire_reseaux(src)
    for r in args.reseau:
        if r not in reseaux:
            raise Erreur(f"réseau « {r} » absent de {p}. Réseaux présents : {', '.join(reseaux)}")
    ponts = {reseaux[r]["bridge"] for r in args.reseau if reseaux[r]["bridge"]}
    tous, actifs = lire_domaines(src)
    autostart = set(lignes(src.virsh("list --all --autostart --name")[1]))
    vms = {}
    for d in tous:
        xml = src.virsh(f"dumpxml --inactive {q(d)}")[1]
        nets, brs = branchements(xml)
        if nets & set(args.reseau) or brs & ponts or d in (args.vm or []):
            vms[d] = xml
    if not vms:
        raise Erreur(f"aucune VM branchée sur {', '.join(args.reseau)}")

    nom = args.nom or args.reseau[0]
    if not NOM_VALIDE.match(nom):
        raise Erreur(f"nom de labo invalide : {nom} (lettres, chiffres, . _ -)")
    dest = f"{stockage.chemin}/{nom}"
    tmp = f"{dest}.en-cours"
    existe = stockage.run(f"test -d {q(dest)}", verifier=False)[0] == 0
    if existe and not args.ecraser:
        raise Erreur(f"le labo « {nom} » existe déjà sur le stockage (ajoutez --ecraser)")

    log(p, f"labo « {nom} » : réseaux {', '.join(args.reseau)} ; VM : {', '.join(vms)}")
    en_marche = [d for d in vms if d in actifs]
    if en_marche and not args.arreter:
        raise Erreur(f"VM allumées : {', '.join(en_marche)}. Éteignez-les ou ajoutez --arreter.")
    for d in en_marche:
        arreter_vm(src, d)

    try:
        stockage.run(f"mkdir -p {q(tmp)}/xml {q(tmp)}/disques")
        ssh_e = f"ssh -i {src.cle} {SSH_OPTS}"
        cible_stock = f"{stockage.utilisateur}@{stockage.ip}"
        manifeste = {"nom": nom, "proprietaire": args.proprietaire or getpass.getuser(),
                     "description": args.description or "", "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "source": src.nom, "reseaux": [], "vms": []}
        for r in args.reseau:
            f = f"xml/reseau-{propre(r)}.xml"
            stockage.ecrire(f"{tmp}/{f}", nettoyer_reseau(reseaux[r]["xml"]))
            manifeste["reseaux"].append({"nom": r, "fichier": f, "pont_origine": reseaux[r]["bridge"]})
        for d, xml in vms.items():
            v = {"nom": d, "fichier": f"xml/vm-{propre(d)}.xml", "autostart": d in autostart,
                 "disques": [], "nvram": None}
            for dk in disques(src, xml):
                if dk["device"] != "disk":
                    continue
                if not dk["source"]:
                    log(p, f"{d} : disque {dk['cible']} sans fichier (type {dk['type']}) → ignoré")
                    continue
                ext = os.path.splitext(dk["source"])[1] or ".img"
                fichier = f"disques/{propre(d)}-{dk['cible']}{ext}"
                log(p, f"{d} : copie de {dk['source']}…")
                fmt = copier_vers(src, dk["source"], f"{cible_stock}:{tmp}/{fichier}", ssh_e)
                v["disques"].append({"cible": dk["cible"], "fichier": fichier, "format": fmt,
                                     "origine": dk["source"]})
            nv = ET.fromstring(xml).find("os/nvram")
            if nv is not None and nv.text:
                fichier = f"disques/{propre(d)}_VARS.fd"
                rc = src.run(src.sudo(f"rsync -a -e {q(ssh_e)} {q(nv.text.strip())} "
                                      f"{q(cible_stock + ':' + tmp + '/' + fichier)}"), verifier=False)[0]
                v["nvram"] = fichier if rc == 0 else None
            stockage.ecrire(f"{tmp}/{v['fichier']}", sans_backing(xml))
            manifeste["vms"].append(v)
        stockage.ecrire(f"{tmp}/labo.json", json.dumps(manifeste, indent=2, ensure_ascii=False))
        stockage.run(f"rm -rf {q(dest)}.ancien && (test -d {q(dest)} && mv {q(dest)} {q(dest)}.ancien || true) "
                     f"&& mv {q(tmp)} {q(dest)} && rm -rf {q(dest)}.ancien")
        log(p, f"sauvegarde terminée → {stockage.ip}:{dest}")
    finally:
        if en_marche and not args.laisser_eteint:
            for d in en_marche:
                src.virsh(f"start {q(d)}", verifier=False)
            log(p, "VM redémarrées")


# --------------------------------------------------------------------------- commande : deployer

def envoyer(stockage, source, h, dest):
    e = f"ssh -i {stockage.cle} {SSH_OPTS}"
    rpath = "sudo -n rsync" if h.mode == "sudo" else "rsync"
    stockage.run(f"rsync -a --sparse --no-owner --no-group -e {q(e)} --rsync-path={q(rpath)} {q(source)} "
                 f"{q(f'{h.utilisateur}@{h.ip}:{dest}')}")


def deployer_sur(h, stockage, man, xml_res, xml_vm, args, tailles=None):
    p = h.nom
    base = f"{stockage.chemin}/{man['nom']}"
    reseaux = lire_reseaux(h)
    liens, nets_hote = interfaces_hote(h)
    tous, _ = lire_domaines(h)

    remplaces = [r["nom"] for r in man["reseaux"] if r["nom"] in reseaux and args.remplacer_reseaux]
    ponts_pris = set(liens) | {i["bridge"] for n, i in reseaux.items() if i["bridge"] and n not in remplaces}
    for n in remplaces:
        ponts_pris.discard(reseaux[n]["bridge"])
    subnets_pris = list(nets_hote) + [s for n, i in reseaux.items() if n not in remplaces for s in i["subnets"]]

    pont_vers_reseau, a_definir, a_demarrer = {}, [], []
    for r in man["reseaux"]:
        nom = r["nom"]
        if nom in reseaux and nom not in remplaces:
            log(p, f"réseau « {nom} » déjà présent (pont {reseaux[nom]['bridge']}) → réutilisé")
            if not reseaux[nom]["actif"]:
                a_demarrer.append(nom)
        else:
            xml, pont = adapter_reseau(xml_res[nom], ponts_pris, subnets_pris, args.conflit_ip, p)
            a_definir.append((nom, xml))
            if pont == r.get("pont_origine"):
                log(p, f"réseau « {nom} » : pont {pont}")
        if r.get("pont_origine"):
            pont_vers_reseau[r["pont_origine"]] = nom

    vms = []
    for v in man["vms"]:
        if v["nom"] in tous and not args.remplacer:
            log(p, f"VM « {v['nom']} » déjà présente → ignorée (--remplacer pour l'écraser)")
        else:
            vms.append(v)

    besoin = besoin_vms(vms, tailles or {})
    if vms and besoin:
        try:
            verifier_place(h, besoin, "labo")
        except Erreur as e:
            if args.simulation:
                log(p, f"SIMULATION : {e}", "erreur")
            raise
        log(p, f"place disponible OK ({taille_lisible(besoin)} nécessaires, "
               f"{taille_lisible(ESPACES.get(h.nom) or 0)} libres)")

    if args.simulation:
        log(p, f"SIMULATION : réseaux à créer {[n for n, _ in a_definir] or '-'} ; "
               f"VM à déployer {[v['nom'] for v in vms] or '-'}")
        return

    for nom, xml in a_definir:
        if nom in remplaces:
            h.virsh(f"net-destroy {q(nom)}", verifier=False)
            h.virsh(f"net-undefine {q(nom)}")
        definir(h, "net", xml)
        h.virsh(f"net-autostart {q(nom)}")
        a_demarrer.append(nom)
    for nom in a_demarrer:
        h.virsh(f"net-start {q(nom)}", verifier=False)

    dossier = f"{h.images}/{man['nom']}"
    if h.run(h.sudo(f"mkdir -p {q(dossier)}"), verifier=False)[0] != 0:
        raise Erreur(f"impossible de créer {dossier} (droits « {h.mode} ») : lancez « Préparer la ferme » sur ce PC")
    for v in vms:
        if v["nom"] in tous:
            log(p, f"VM « {v['nom']} » existante → supprimée pour remplacement")
            supprimer_vm(h, v["nom"])
        chemins, formats = {}, {}
        for d in v["disques"]:
            dest = f"{dossier}/{os.path.basename(d['fichier'])}"
            log(p, f"{v['nom']} : réception de {os.path.basename(d['fichier'])}…")
            envoyer(stockage, f"{base}/{d['fichier']}", h, dest)
            chemins[d["cible"]], formats[d["cible"]] = dest, d.get("format")
        nvram = None
        if v.get("nvram"):
            nvram = f"{dossier}/{os.path.basename(v['nvram'])}"
            envoyer(stockage, f"{base}/{v['nvram']}", h, nvram)
        definir(h, "dom", adapter_vm(xml_vm[v["nom"]], chemins, formats, nvram, pont_vers_reseau, h, p))
        if v.get("autostart"):
            h.virsh(f"autostart {q(v['nom'])}", verifier=False)
        if args.demarrer:
            h.virsh(f"start {q(v['nom'])}", verifier=False)
        log(p, f"VM « {v['nom']} » déployée")
    log(p, "terminé")


def cmd_deployer(args, stockage, hotes):
    cibles = choisir_hotes(hotes, args)
    man = lire_manifeste(stockage, args.labo)
    base = f"{stockage.chemin}/{args.labo}"
    xml_res = {r["nom"]: stockage.sortie(f"cat {q(base)}/{q(r['fichier'])}") for r in man["reseaux"]}
    xml_vm = {v["nom"]: stockage.sortie(f"cat {q(base)}/{q(v['fichier'])}") for v in man["vms"]}
    man["nom"] = args.labo
    tailles = tailles_fichiers(stockage, man)
    log("maître", f"déploiement de « {man['nom']} » ({man.get('proprietaire', '?')}, {len(man['vms'])} VM) "
                  f"sur {len(cibles)} serveur(s) : {', '.join(h.nom for h in cibles)}")
    log("maître", f"place nécessaire par PC : {taille_lisible(besoin_vms(man['vms'], tailles))} "
                  f"(+ {taille_lisible(MARGE_DISQUE)} de marge)")
    ok, ko = en_parallele(cibles, lambda h: deployer_sur(h, stockage, man, xml_res, xml_vm, args, tailles),
                          args.parallele)
    log("maître", f"bilan : {len(ok)} réussi(s){', ' + str(len(ko)) + ' échec(s) : ' + ', '.join(ko) if ko else ''}")
    return ok, ko


# --------------------------------------------------------------------------- commande : retirer

def retirer_de(h, man, args):
    tous, _ = lire_domaines(h)
    for v in man["vms"]:
        if v["nom"] in tous:
            supprimer_vm(h, v["nom"])
            log(h.nom, f"VM « {v['nom']} » supprimée")
    if propre(man["nom"]) and h.images.startswith("/"):
        h.run(h.sudo(f"rm -rf {q(h.images + '/' + man['nom'])}"))
    if args.garder_reseaux:
        return
    restants, _ = lire_domaines(h)
    utilises = set()
    for d in restants:
        utilises |= branchements(h.virsh(f"dumpxml --inactive {q(d)}")[1])[0]
    presents = lire_reseaux(h)
    for r in man["reseaux"]:
        if r["nom"] not in presents:
            continue
        if r["nom"] in utilises:
            log(h.nom, f"réseau « {r['nom']} » encore utilisé par d'autres VM → conservé")
            continue
        h.virsh(f"net-destroy {q(r['nom'])}", verifier=False)
        h.virsh(f"net-undefine {q(r['nom'])}")
        log(h.nom, f"réseau « {r['nom']} » supprimé")


def cmd_retirer(args, stockage, hotes):
    cibles = choisir_hotes(hotes, args)
    man = lire_manifeste(stockage, args.labo)
    if not args.oui:
        rep = input(f"Supprimer le labo « {man['nom']} » (VM et disques) de "
                    f"{', '.join(h.nom for h in cibles)} ? [o/N] ")
        if rep.strip().lower() not in ("o", "oui", "y"):
            return
    ok, ko = en_parallele(cibles, lambda h: retirer_de(h, man, args), args.parallele)
    log("maître", f"bilan : {len(ok)} réussi(s), {len(ko)} échec(s)")
    return ok, ko


# --------------------------------------------------------------------------- vue de la ferme

def inventaire_hote(h):
    """Ce qui est présent sur un PC : VM (en marche ?), réseaux (actifs ?), dossiers de labos déployés."""
    tous, actifs = lire_domaines(h)
    nets = lignes(h.virsh("net-list --all --name")[1])
    nets_on = set(lignes(h.virsh("net-list --name")[1]))
    dossiers = lignes(h.run(f"ls -1 {q(h.images)} 2>/dev/null", verifier=False)[1])
    return {"vms": {d: d in actifs for d in tous}, "reseaux": {n: n in nets_on for n in nets},
            "dossiers": set(dossiers)}


def etat_labo(man, inv):
    """État d'un labo du stockage sur un PC (None = absent)."""
    noms = [v["nom"] for v in man.get("vms", [])]
    presentes = [n for n in noms if n in inv["vms"]]
    reseaux_manquants = [r["nom"] for r in man.get("reseaux", []) if r["nom"] not in inv["reseaux"]]
    if not presentes and man["nom"] not in inv["dossiers"]:
        return None
    return {"presentes": presentes, "total": len(noms),
            "manquantes": [n for n in noms if n not in inv["vms"]],
            "en_marche": [n for n in presentes if inv["vms"][n]],
            "reseaux_manquants": reseaux_manquants,
            "complet": len(presentes) == len(noms) and not reseaux_manquants}


def hors_labos(labos, inv):
    """VM et réseaux d'un PC qui n'appartiennent à aucun labo du stockage."""
    vms = {v["nom"] for m in labos for v in m.get("vms", [])}
    nets = {r["nom"] for m in labos for r in m.get("reseaux", [])} | {"default"}
    return sorted(set(inv["vms"]) - vms), sorted(set(inv["reseaux"]) - nets)


# --------------------------------------------------------------------------- vue d'une machine

ETATS_VM = {"running": "en marche", "shut off": "arrêtée", "paused": "en pause", "idle": "en marche",
            "in shutdown": "arrêt en cours", "crashed": "plantée", "pmsuspended": "en veille",
            "blocked": "en marche"}


def _champs(texte):
    """Sortie « Clé:   valeur » de virsh (dominfo, net-info…) → dict."""
    d = {}
    for l in texte.splitlines():
        k, sep, v = l.partition(":")
        if sep:
            d[k.strip()] = v.strip()
    return d


def details_machine(h):
    """Réseaux et VM d'un PC, avec leur état, pour la vue machine."""
    reseaux = []
    auto_nets = set(lignes(h.virsh("net-list --all --autostart --name")[1]))
    for n, i in sorted(lire_reseaux(h).items()):
        reseaux.append({"nom": n, "actif": i["actif"], "autostart": n in auto_nets, "pont": i["bridge"],
                        "mode": i["mode"], "subnets": [str(x) for x in i["subnets"]]})
    vms = []
    for d in sorted(lignes(h.virsh("list --all --name")[1]), key=str.lower):
        info = _champs(h.virsh(f"dominfo {q(d)}", verifier=False)[1])
        etat = info.get("State", "?")
        try:
            ram = int(info.get("Max memory", "0").split()[0]) * 1024
        except ValueError:
            ram = 0
        snaps = lignes(h.virsh(f"snapshot-list --name {q(d)}", verifier=False)[1])
        nets = branchements(h.virsh(f"dumpxml --inactive {q(d)}")[1])[0]
        vms.append({"nom": d, "etat": etat, "etat_fr": ETATS_VM.get(etat, etat), "vcpu": info.get("CPU(s)", "?"),
                    "ram": ram, "autostart": info.get("Autostart") == "enable", "snapshots": len(snaps),
                    "reseaux": sorted(nets)})
    return {"reseaux": reseaux, "vms": vms, "libre": espace_libre(h, h.images)}


ACTIONS_VM = {"demarrer": "start", "arreter": "shutdown", "forcer": "destroy", "redemarrer": "reboot",
              "pause": "suspend", "reprendre": "resume"}
ACTIONS_RESEAU = {"demarrer": "net-start", "arreter": "net-destroy"}


def action_vm(h, vm, action):
    if action in ("autostart-on", "autostart-off"):
        h.virsh(f"autostart {'--disable ' if action == 'autostart-off' else ''}{q(vm)}")
    elif action == "supprimer":
        h.virsh(f"destroy {q(vm)}", verifier=False)
        if h.virsh(f"undefine --nvram --snapshots-metadata --remove-all-storage {q(vm)}", verifier=False)[0] != 0:
            h.virsh(f"undefine --snapshots-metadata --remove-all-storage {q(vm)}")
    else:
        h.virsh(f"{ACTIONS_VM[action]} {q(vm)}")
    log(h.nom, f"VM « {vm} » : {action}")


def action_reseau(h, reseau, action):
    if action in ("autostart-on", "autostart-off"):
        h.virsh(f"net-autostart {'--disable ' if action == 'autostart-off' else ''}{q(reseau)}")
    else:
        h.virsh(f"{ACTIONS_RESEAU[action]} {q(reseau)}")
    log(h.nom, f"réseau « {reseau} » : {action}")


def lister_snapshots(h, vm):
    courant = h.virsh(f"snapshot-current --name {q(vm)}", verifier=False)[1].strip()
    snaps = []
    for l in h.virsh(f"snapshot-list {q(vm)}")[1].splitlines():
        cols = re.split(r"\s{2,}", l.strip())
        if len(cols) >= 3 and not set(cols[0]) <= {"-"} and cols[0] != "Name":
            snaps.append({"nom": cols[0], "date": cols[1][:19], "etat": ETATS_VM.get(cols[2], cols[2]),
                          "courant": cols[0] == courant})
    return snaps


def creer_snapshot(h, vm, nom, description=""):
    if not NOM_VALIDE.match(nom):
        raise Erreur("nom d'instantané invalide (lettres, chiffres, . _ -)")
    h.virsh(f"snapshot-create-as {q(vm)} {q(nom)}" + (f" --description {q(description)}" if description else ""))
    log(h.nom, f"VM « {vm} » : instantané « {nom} » créé")


def restaurer_snapshot(h, vm, nom):
    h.virsh(f"snapshot-revert {q(vm)} {q(nom)}")
    log(h.nom, f"VM « {vm} » : retour à l'instantané « {nom} »")


def supprimer_snapshot(h, vm, nom):
    h.virsh(f"snapshot-delete {q(vm)} {q(nom)}")
    log(h.nom, f"VM « {vm} » : instantané « {nom} » supprimé")


def uri_libvirt(h):
    """Adresse libvirt pour virt-manager / virt-viewer lancés depuis ce poste."""
    if h.local:
        return "qemu:///system"
    cle = os.path.expanduser(f"~/.ssh/{NOM_CLE}")
    return f"qemu+ssh://{h.utilisateur}@{h.ip}/system?keyfile={cle}&no_verify=1"


# --------------------------------------------------------------------------- images ISO

def nom_iso_valide(nom):
    return bool(nom) and "/" not in nom and not nom.startswith(".") and nom.lower().endswith(".iso")


def taille_lisible(octets):
    for unite in ("o", "Ko", "Mo", "Go", "To"):
        if octets < 1024 or unite == "To":
            return f"{octets:.0f} {unite}" if unite in ("o", "Ko") else f"{octets:.1f} {unite}"
        octets /= 1024


def lister_isos(stockage):
    """ISO présentes dans le dossier ISO du stockage : liste de dicts (nom, taille, date)."""
    d = stockage.chemin_iso
    out = stockage.sortie(f"mkdir -p {q(d)} && cd {q(d)} && for f in *; do [ -f \"$f\" ] && "
                          f"case \"$f\" in *.iso|*.ISO|*.Iso) stat -c '%s|%Y|%n' \"$f\";; esac; done; true")
    isos = []
    for l in lignes(out):
        try:
            taille, date, nom = l.split("|", 2)
            isos.append({"nom": nom, "taille": int(taille),
                         "date": datetime.fromtimestamp(int(date)).strftime("%Y-%m-%d %H:%M")})
        except ValueError:
            detail("stockage", f"ligne ISO ignorée : {l}")
    return sorted(isos, key=lambda i: i["nom"].lower())


def verifier_dossier_iso(stockage, chemin):
    """Crée si besoin et vérifie un dossier ISO sur le stockage (utilisé avant de changer l'emplacement)."""
    if not chemin.startswith("/"):
        raise Erreur("l'emplacement des ISO doit être un chemin absolu (commençant par /)")
    if stockage.run(f"mkdir -p {q(chemin)} && test -w {q(chemin)}", verifier=False)[0] != 0:
        raise Erreur(f"impossible de créer ou d'écrire dans {chemin} sur le stockage "
                     f"(compte {stockage.utilisateur})")
    return chemin.rstrip("/")


def envoyer_iso(stockage, fichier):
    """Copie une ISO de ce poste vers le dossier ISO du stockage (reprise possible si interrompue)."""
    nom = os.path.basename(fichier)
    if not nom_iso_valide(nom):
        raise Erreur(f"« {nom} » n'est pas un fichier .iso")
    if not os.path.isfile(fichier):
        raise Erreur(f"fichier introuvable : {fichier}")
    local = hote_local()
    stockage.run(f"mkdir -p {q(stockage.chemin_iso)}")
    log("local", f"envoi de {nom} ({taille_lisible(os.path.getsize(fichier))}) vers le stockage…")
    local.run(f"rsync -a -s --partial --chmod=F644 -e {q(f'ssh -i {local.cle} {SSH_OPTS}')} {q(fichier)} "
              f"{q(f'{stockage.utilisateur}@{stockage.ip}:{stockage.chemin_iso}/')}")
    log("local", f"{nom} envoyée sur le stockage", "succes")


def supprimer_iso(stockage, nom):
    if not nom_iso_valide(nom):
        raise Erreur(f"nom d'ISO invalide : {nom}")
    stockage.run(f"rm -f {q(stockage.chemin_iso + '/' + nom)}")
    log("stockage", f"{nom} supprimée")


def assurer_pool_iso(h):
    """Pool libvirt « labferme-iso » sur le dossier des ISO, pour les retrouver dans virt-manager."""
    rc, xml, _ = h.virsh(f"pool-dumpxml {POOL_ISO}", verifier=False)
    if rc != 0:
        h.virsh(f"pool-define-as {POOL_ISO} dir --target {q(h.isos)}")
        h.virsh(f"pool-autostart {POOL_ISO}", verifier=False)
        log(h.nom, f"pool de stockage libvirt « {POOL_ISO} » créé ({h.isos})")
    elif f"<path>{h.isos}</path>" not in xml:
        log(h.nom, f"le pool « {POOL_ISO} » existe avec un autre dossier → laissé tel quel", "alerte")
    h.virsh(f"pool-start {POOL_ISO}", verifier=False)
    h.virsh(f"pool-refresh {POOL_ISO}", verifier=False)


def copier_iso_vers(stockage, nom, h):
    """Le PC h télécharge l'ISO depuis le stockage (avec sa propre clé), dans son dossier ISO."""
    if not nom_iso_valide(nom):
        raise Erreur(f"nom d'ISO invalide : {nom}")
    if h.run(h.sudo(f"mkdir -p {q(h.isos)}"), verifier=False)[0] != 0:
        raise Erreur(f"impossible de créer {h.isos} : " + (
            "clic droit sur « Ce poste » → « Préparer ce poste… »" if h.local else "lancez « Préparer » sur ce PC"))
    taille = stockage.sortie(f"stat -c %s {q(stockage.chemin_iso + '/' + nom)}", verifier=False).strip()
    deja = h.run(f"test -f {q(h.isos + '/' + nom)}", verifier=False)[0] == 0
    if taille.isdigit() and not deja:
        verifier_place(h, int(taille), "iso")
    log(h.nom, f"téléchargement de {nom}…")
    source = f"{stockage.utilisateur}@{stockage.ip}:{stockage.chemin_iso}/{nom}"
    h.run(h.sudo(f"rsync -a -s --partial --no-owner --no-group --chmod=F644 "
                 f"-e {q(f'ssh -i {h.cle} {SSH_OPTS}')} {q(source)} {q(h.isos + '/')}"))
    try:
        assurer_pool_iso(h)
    except Erreur as e:
        log(h.nom, f"ISO copiée mais pool libvirt non créé : {e}", "alerte")
    log(h.nom, f"{nom} disponible dans {h.isos}", "succes")


def cmd_isos(args, stockage, hotes):
    isos = lister_isos(stockage)
    print(f"Dossier ISO : {stockage.ip}:{stockage.chemin_iso}")
    for i in isos:
        print(f"  {i['nom']:<50} {taille_lisible(i['taille']):>10}  {i['date']}")
    if not isos:
        print("  (aucune ISO)")


def cmd_iso_envoyer(args, stockage, hotes):
    for f in args.fichier:
        envoyer_iso(stockage, f)


def cmd_iso_copier(args, stockage, hotes):
    cibles = [] if (args.local and not (args.tous or args.pool or args.hote)) else choisir_hotes(hotes, args)
    if args.local:
        cibles.append(hote_local())
    if args.nom not in {i["nom"] for i in lister_isos(stockage)}:
        raise Erreur(f"ISO « {args.nom} » absente du stockage (commande « isos »)")
    ok, ko = en_parallele(cibles, lambda h: copier_iso_vers(stockage, args.nom, h), args.parallele)
    log("maître", f"bilan : {len(ok)} réussi(s), {len(ko)} échec(s)")
    return ok, ko


def cmd_iso_supprimer(args, stockage, hotes):
    supprimer_iso(stockage, args.nom)


# --------------------------------------------------------------------------- arguments

def ajouter_selection(sp):
    g = sp.add_argument_group("sélection des serveurs")
    g.add_argument("--tous", action="store_true", help="tous les serveurs de la ferme")
    g.add_argument("--pool", action="append", metavar="NOM", help="serveurs d'un pool (répétable)")
    g.add_argument("--hote", action="append", metavar="NOM", help="serveur par nom ou IP (répétable)")
    sp.add_argument("--parallele", type=int, default=4, metavar="N", help="serveurs traités en même temps (4)")


def construire_parser():
    p = argparse.ArgumentParser(description="Sauvegarde et déploiement de labos KVM sur une ferme Debian.")
    p.add_argument("--version", action="version", version=f"Ferme KVM {__version__} ({DATE_VERSION})")
    p.add_argument("-c", "--config", default=os.environ.get("LABFERME_CONFIG", "ferme.yaml"),
                   help="fichier de configuration (ferme.yaml)")
    sub = p.add_subparsers(dest="commande", required=True)

    s = sub.add_parser("preparer", help="clés SSH, sudo, paquets (à faire une fois)")
    ajouter_selection(s)
    s.add_argument("--local", action="store_true", help="configurer aussi sudo sur ce poste (pour sauvegarder)")

    s = sub.add_parser("lister", help="labos disponibles sur le stockage")
    s.add_argument("-d", "--details", action="store_true")

    s = sub.add_parser("inventaire", help="réseaux et VM présents sur la ferme")
    ajouter_selection(s)

    s = sub.add_parser("sauvegarder", help="sauvegarder un labo vers le stockage")
    s.add_argument("-r", "--reseau", action="append", required=True,
                   help="réseau virtuel du labo (répétable si le labo a plusieurs réseaux)")
    s.add_argument("--vm", action="append", help="VM supplémentaire à inclure (répétable)")
    s.add_argument("-n", "--nom", help="nom du labo sur le stockage (défaut : nom du 1er réseau)")
    s.add_argument("--proprietaire", help="enseignant responsable (défaut : utilisateur courant)")
    s.add_argument("--description")
    s.add_argument("--hote", help="sauvegarder depuis ce serveur de la ferme au lieu de ce poste")
    s.add_argument("--arreter", action="store_true", help="éteindre proprement les VM allumées puis les relancer")
    s.add_argument("--laisser-eteint", action="store_true", help="avec --arreter : ne pas relancer les VM")
    s.add_argument("--ecraser", action="store_true", help="remplacer une sauvegarde existante du même nom")

    s = sub.add_parser("deployer", help="déployer un labo du stockage vers la ferme")
    s.add_argument("labo")
    ajouter_selection(s)
    s.add_argument("--remplacer", action="store_true", help="écraser les VM du même nom déjà présentes")
    s.add_argument("--remplacer-reseaux", action="store_true", help="redéfinir les réseaux du même nom")
    s.add_argument("--conflit-ip", choices=["arreter", "decaler", "sans-ip"], default="arreter",
                   help="si le sous-réseau est déjà utilisé sur la cible (défaut : arreter)")
    s.add_argument("--demarrer", action="store_true", help="démarrer les VM après déploiement")
    s.add_argument("--simulation", action="store_true", help="afficher ce qui serait fait, sans rien modifier")

    s = sub.add_parser("retirer", help="supprimer un labo des serveurs")
    s.add_argument("labo")
    ajouter_selection(s)
    s.add_argument("--garder-reseaux", action="store_true")
    s.add_argument("--oui", action="store_true", help="ne pas demander de confirmation")

    sub.add_parser("isos", help="lister les ISO du stockage")
    s = sub.add_parser("iso-envoyer", help="envoyer des ISO de ce poste vers le stockage")
    s.add_argument("fichier", nargs="+")
    s = sub.add_parser("iso-copier", help="copier une ISO du stockage vers des serveurs / ce poste")
    s.add_argument("nom")
    ajouter_selection(s)
    s.add_argument("--local", action="store_true", help="copier aussi sur ce poste")
    s = sub.add_parser("iso-supprimer", help="supprimer une ISO du stockage")
    s.add_argument("nom")
    return p


COMMANDES = {"preparer": cmd_preparer, "lister": cmd_lister, "inventaire": cmd_inventaire,
             "sauvegarder": cmd_sauvegarder, "deployer": cmd_deployer, "retirer": cmd_retirer,
             "isos": cmd_isos, "iso-envoyer": cmd_iso_envoyer, "iso-copier": cmd_iso_copier,
             "iso-supprimer": cmd_iso_supprimer}


def main():
    args = construire_parser().parse_args()
    init_journal()
    detail("cli", f"Ferme KVM {__version__} – " + " ".join(sys.argv))
    try:
        stockage, hotes = charger_config(args.config)
        res = COMMANDES[args.commande](args, stockage, hotes)
        if isinstance(res, tuple) and res[1]:
            sys.exit(1)
    except Erreur as e:
        log("ERREUR", str(e))
        print(f"(détails dans {FICHIER_LOG})", file=sys.stderr)
        sys.exit(1)
    except Exception:
        detail("cli", traceback.format_exc())
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        try:
            stockage.fermer()
        except NameError:
            pass


if __name__ == "__main__":
    main()
