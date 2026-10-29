# -*- coding: utf-8 -*-
"""Tests du moteur (labferme.py) avec de fausses machines : aucune connexion SSH, aucun libvirt réel.
    python3 -m unittest discover -s tests -v
"""

import argparse
import contextlib
import io
import json
import logging
import os
import re
import tempfile
import unittest
from unittest import mock

from fakes import FauxHote, L, machine_avec_labo, xml_vm

L.RAPPEL_LOG = lambda ligne, niveau="info": None          # pas de bruit dans la sortie des tests
L._journal.addHandler(logging.NullHandler())
L._journal.propagate = False

IMAGES = L.IMAGES_DEFAUT


def lien_pret(lanceur, distant):
    """Rend la liaison SSH + rsync « fonctionnelle » du point de vue de la fausse machine qui lance rsync."""
    lanceur.etat["existe"].add(lanceur.cle)
    lanceur.reponses.append((r"echo labferme-ok", (0, "labferme-ok\n", "")))


def args_transfert(**kw):
    base = dict(reseau=["lab1"], vm=None, nom=None, depuis=None, source=None, cibles=None, vers=None, tous=False,
                pool=None, parallele=2, arreter=False, laisser_eteint=False, remplacer=False,
                remplacer_reseaux=False, conflit_ip="arreter", demarrer=False, simulation=False, sources=())
    base.update(kw)
    return argparse.Namespace(**base)


class TestRetrait(unittest.TestCase):
    """Correctif : le nom du dossier fait foi, jamais celui de labo.json."""

    def setUp(self):
        self.pc = FauxHote("pc1", utilisateur="enseignant")
        self.stockage = FauxHote("stockage")
        self.stockage.chemin = "/srv/labos"

    def manifeste(self, nom_json):
        man = {"nom": nom_json, "reseaux": [{"nom": "lab1", "fichier": "xml/reseau-lab1.xml"}],
               "vms": [{"nom": "vm1", "fichier": "xml/vm-vm1.xml", "disques": []}]}
        self.stockage.reponses.append((r"cat /srv/labos/\S+/labo\.json", (0, json.dumps(man), "")))

    def test_le_nom_du_dossier_fait_foi(self):
        self.manifeste("../../etc")                          # labo.json altéré
        args = argparse.Namespace(labo="lab1", tous=False, pool=None, hote=["pc1"], parallele=1,
                                  garder_reseaux=False, oui=True)
        L.cmd_retirer(args, self.stockage, [self.pc])
        rm = self.pc.commandes(r"rm -rf")
        self.assertEqual(len(rm), 1)
        self.assertIn(f"{IMAGES}/lab1", rm[0])
        self.assertNotIn("etc", rm[0])

    def test_nom_invalide_refuse_avant_toute_suppression(self):
        for nom in ("../x", "a/b", ".caché", "", None):
            pc = FauxHote("pc1")
            with self.assertRaises(L.Erreur):
                L.retirer_de(pc, {"nom": nom, "vms": [], "reseaux": []}, argparse.Namespace(garder_reseaux=True))
            self.assertEqual(pc.journal, [], f"aucune commande attendue pour {nom!r}")

    def test_dossier_images_relatif_refuse(self):
        pc = FauxHote("pc1", images="images/labos")
        with self.assertRaises(L.Erreur):
            L.retirer_de(pc, {"nom": "lab1", "vms": [], "reseaux": []}, argparse.Namespace(garder_reseaux=True))
        self.assertEqual(pc.journal, [])


class TestExport(unittest.TestCase):
    """Correctif : un export interrompu ne laisse pas de dossier .en-cours ; la refactorisation lire_labo
    ne change pas le contenu exporté."""

    def setUp(self):
        self.stockage = FauxHote("stockage")
        self.stockage.chemin = "/srv/labos"
        self.src = machine_avec_labo("src", actives=("vm1",))

    def args(self, **kw):
        base = dict(reseau=["lab1"], vm=None, nom=None, hote=None, source=self.src, proprietaire="moi",
                    description="d", arreter=True, laisser_eteint=False, ecraser=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_export_reussi(self):
        L.cmd_sauvegarder(self.args(), self.stockage, [])
        ecrits = self.stockage.etat["ecrits"]
        man = json.loads(next(v for k, v in ecrits.items() if k.endswith("labo.json")))
        self.assertEqual([v["nom"] for v in man["vms"]], ["vm1", "vm2"])
        self.assertEqual(man["vms"][0]["disques"][0]["fichier"], "disques/vm1-vda.qcow2")
        self.assertEqual(man["reseaux"][0]["pont_origine"], "virbr1")
        mv = self.stockage.commandes(r"mv /srv/labos/lab1\.en-cours /srv/labos/lab1")
        self.assertEqual(len(mv), 1)
        self.assertEqual(self.stockage.commandes(r"rm -rf /srv/labos/lab1\.en-cours$"), [],
                         "rien à nettoyer après un export réussi")
        self.assertIn("vm1", self.src.etat["actifs"], "VM relancée")

    def test_echec_nettoie_le_dossier_temporaire(self):
        self.src.echecs.append(r"rsync")
        with self.assertRaises(L.Erreur):
            L.cmd_sauvegarder(self.args(), self.stockage, [])
        nettoyage = self.stockage.commandes(r"rm -rf /srv/labos/lab1\.en-cours$")
        self.assertEqual(len(nettoyage), 1)
        self.assertEqual(self.stockage.commandes(r"mv /srv/labos/lab1\.en-cours /srv/labos/lab1"), [],
                         "l'export raté ne doit jamais devenir un labo")
        self.assertIn("vm1", self.src.etat["actifs"], "la VM éteinte pour l'export est relancée malgré l'échec")

    def test_depart_sur_dossier_vide(self):
        L.cmd_sauvegarder(self.args(), self.stockage, [])
        creation = self.stockage.commandes(r"mkdir -p /srv/labos/lab1\.en-cours/xml")[0]
        self.assertTrue(creation.startswith("rm -rf /srv/labos/lab1.en-cours && mkdir"))

    def test_perte_de_connexion_pendant_le_nettoyage_ne_masque_pas_l_erreur(self):
        self.src.echecs.append(r"rsync")
        vraie_run = self.stockage.run

        def run(cmd, entree=None, verifier=True):
            if cmd.startswith("rm -rf /srv/labos/lab1.en-cours") and "mkdir" not in cmd:
                raise OSError("socket fermée")
            return vraie_run(cmd, entree, verifier)
        self.stockage.run = run
        with self.assertRaises(L.Erreur) as cm:                # l'erreur d'origine, pas OSError
            L.cmd_sauvegarder(self.args(), self.stockage, [])
        self.assertIn("rsync", str(cm.exception))


class TestCopierVers(unittest.TestCase):
    """Non-régression : la refactorisation (aplatir) émet exactement les mêmes commandes qu'en 1.1.0."""

    def test_sans_image_de_base(self):
        h = FauxHote("src")
        ch = "/var/lib/libvirt/images/vm1.qcow2"
        h.etat["disques"][ch] = {"format": "qcow2"}
        self.assertEqual(L.copier_vers(h, ch, "stock@10.0.0.9:/srv/x/vm1-vda.qcow2", "ssh -i /k -o A"), "qcow2")
        self.assertEqual([c for c, _ in h.journal], [
            f"sudo -n qemu-img info -U --output=json {ch}",
            f"sudo -n rsync -a --sparse -e 'ssh -i /k -o A' {ch} stock@10.0.0.9:/srv/x/vm1-vda.qcow2"])

    def test_avec_image_de_base(self):
        h = FauxHote("src")
        ch = "/var/lib/libvirt/images/vm1.qcow2"
        h.etat["disques"][ch] = {"format": "qcow2", "backing-filename": "/base.qcow2"}
        L.copier_vers(h, ch, "stock@10.0.0.9:/srv/x/vm1-vda.qcow2", "ssh -i /k -o A")
        tmp = f"/var/tmp/labferme-{os.getpid()}-vm1.qcow2.qcow2"
        self.assertEqual([c for c, _ in h.journal], [
            f"sudo -n qemu-img info -U --output=json {ch}",
            f"sudo -n qemu-img convert -U -O qcow2 {ch} {tmp}",
            f"sudo -n rsync -a --sparse -e 'ssh -i /k -o A' {tmp} stock@10.0.0.9:/srv/x/vm1-vda.qcow2",
            f"sudo -n rm -f {tmp}"])


class TestTransfert(unittest.TestCase):
    """Déploiement direct machine → machine, sans stockage."""

    def setUp(self):
        self.src = machine_avec_labo("src", ip="10.0.0.1")
        self.pc1 = FauxHote("pc1", ip="10.0.0.2")
        self.pc2 = FauxHote("pc2", ip="10.0.0.3")
        for pc in (self.pc1, self.pc2):
            lien_pret(self.src, pc)

    def lancer(self, cibles=None, **kw):
        a = args_transfert(source=self.src, cibles=cibles or [self.pc1], **kw)
        return L.cmd_transferer(a, None, [])

    def test_deploiement_sur_une_cible(self):
        ok, ko = self.lancer()
        self.assertEqual((ok, ko), (["pc1"], []))
        self.assertEqual(sorted(self.pc1.etat["domaines"]), ["vm1", "vm2"])
        self.assertIn("lab1", self.pc1.etat["reseaux"])
        xml = self.pc1.etat["domaines"]["vm1"]
        self.assertIn(f"<source file=\"{IMAGES}/lab1/vm1-vda.qcow2\"", xml.replace("'", '"'))
        self.assertNotIn("<uuid>", xml)

    def test_les_disques_partent_de_la_source_vers_la_cible(self):
        self.lancer()
        rsync = [c for c in self.src.commandes(r"rsync -a") if "10.0.0.2" in c]
        self.assertEqual(len(rsync), 2)                        # un disque par VM
        self.assertIn("--rsync-path='sudo -n rsync'", rsync[0])
        self.assertTrue(rsync[0].startswith("sudo -n rsync"))
        self.assertIn(f"enseignant@10.0.0.2:{IMAGES}/lab1/", rsync[0])
        self.assertEqual(self.pc1.commandes(r"rsync -a"), [], "la cible ne lance pas rsync")

    def test_aucun_acces_au_stockage(self):
        # stockage=None : si le moteur y touchait, AttributeError. On vérifie aussi qu'aucune commande n'y mène.
        self.lancer()
        self.assertEqual(self.src.commandes(r"/srv/labos"), [])

    def test_plusieurs_cibles(self):
        ok, ko = self.lancer(cibles=[self.pc1, self.pc2])
        self.assertEqual(sorted(ok), ["pc1", "pc2"])
        self.assertEqual(sorted(self.pc2.etat["domaines"]), ["vm1", "vm2"])

    def test_disque_avec_image_de_base_aplati_une_seule_fois(self):
        for v in ("vm1", "vm2"):
            ch = f"/var/lib/libvirt/images/{v}.qcow2"
            self.src.etat["disques"][ch] = {"format": "qcow2", "backing-filename": "/base.qcow2",
                                            "actual-size": 2 * 1024 ** 3}
        self.lancer(cibles=[self.pc1, self.pc2])
        convert = self.src.commandes(r"qemu-img convert")
        self.assertEqual(len(convert), 2, "un aplatissement par disque, pas par cible")
        tmps = [c.split()[-1] for c in convert]
        self.assertEqual(len(set(tmps)), 2, "deux fichiers temporaires distincts")
        for t in tmps:
            self.assertEqual(len([c for c in self.src.commandes(r"rsync -a") if f" {t} " in c]), 2,
                             "chaque disque aplati est envoyé aux deux cibles")
            self.assertEqual(len(self.src.commandes(rf"rm -f {re.escape(t)}$")), 1, "temporaire supprimé")
        self.assertIn("type='qcow2'", self.pc1.etat["domaines"]["vm1"].replace('"', "'"))
        self.assertNotIn("backingStore", self.pc1.etat["domaines"]["vm1"])

    def test_place_insuffisante_dans_var_tmp_avant_toute_modification(self):
        ch = "/var/lib/libvirt/images/vm1.qcow2"
        self.src.etat["disques"][ch] = {"format": "qcow2", "backing-filename": "/b", "actual-size": 50 * 1024 ** 3}
        self.src.etat["actifs"] = {"vm1"}
        with self.assertRaisesRegex(L.Erreur, "place insuffisante dans /var/tmp"):
            self.lancer(arreter=True)
        self.assertEqual(self.src.commandes(r"shutdown"), [], "aucune VM éteinte si le transfert est impossible")

    def test_place_insuffisante_sur_la_cible(self):
        self.pc1.etat["libre_ko"] = 1024                        # 1 Mo libre
        ok, ko = self.lancer()
        self.assertEqual((ok, ko), ([], ["pc1"]))
        self.assertEqual(self.src.commandes(r"rsync -a"), [])

    def test_vm_allumees_sans_arret_refuse(self):
        self.src.etat["actifs"] = {"vm1"}
        with self.assertRaisesRegex(L.Erreur, "VM allumées"):
            self.lancer()
        self.assertEqual(self.src.commandes(r"rsync -a"), [])

    def test_vm_allumees_eteintes_puis_relancees_apres_la_copie(self):
        self.src.etat["actifs"] = {"vm1"}
        self.lancer(arreter=True)
        cmds = [c for c, _ in self.src.journal]
        i_stop = next(i for i, c in enumerate(cmds) if "shutdown vm1" in c)
        i_copie = next(i for i, c in enumerate(cmds) if "rsync -a" in c)
        i_start = next(i for i, c in enumerate(cmds) if re.search(r"virsh .* start vm1", c))
        self.assertLess(i_stop, i_copie)
        self.assertLess(max(i for i, c in enumerate(cmds) if "rsync -a" in c), i_start)
        self.assertIn("vm1", self.src.etat["actifs"])

    def test_vm_relancee_meme_si_la_copie_echoue(self):
        self.src.etat["actifs"] = {"vm1"}
        self.src.echecs.append(r"rsync -a")
        ok, ko = self.lancer(arreter=True)
        self.assertEqual(ko, ["pc1"])
        self.assertIn("vm1", self.src.etat["actifs"])

    def test_laisser_eteint(self):
        self.src.etat["actifs"] = {"vm1"}
        self.lancer(arreter=True, laisser_eteint=True)
        self.assertNotIn("vm1", self.src.etat["actifs"])

    def test_simulation_ne_modifie_rien(self):
        self.src.etat["actifs"] = {"vm1"}
        ok, ko = self.lancer(simulation=True)
        self.assertEqual(ko, [])
        self.assertEqual(self.pc1.etat["domaines"], {})
        self.assertEqual(self.pc1.etat["reseaux"], {})
        self.assertEqual(self.src.commandes(r"rsync -a|shutdown|convert"), [])
        self.assertIn("vm1", self.src.etat["actifs"])

    def test_liaison_non_prete_refuse_avant_d_eteindre_quoi_que_ce_soit(self):
        self.src.reponses.insert(0, (r"echo labferme-ok", (255, "", "Permission denied (publickey)")))
        self.src.etat["actifs"] = {"vm1"}
        with self.assertRaisesRegex(L.Erreur, "liaison non prête"):
            self.lancer(arreter=True)
        self.assertEqual(self.src.commandes(r"shutdown|rsync -a"), [])
        self.assertEqual(self.pc1.etat["domaines"], {})

    def test_droits_insuffisants_sur_la_cible(self):
        self.pc1._mode = "groupe"
        with self.assertRaisesRegex(L.Erreur, "groupe libvirt seulement"):
            self.lancer()

    def test_vm_deja_presente_ignoree_sans_remplacer(self):
        self.pc1.etat["domaines"]["vm1"] = xml_vm("vm1", "lab1")
        avant = self.pc1.etat["domaines"]["vm1"]
        self.lancer()
        self.assertEqual(self.pc1.etat["domaines"]["vm1"], avant)
        self.assertIn("vm2", self.pc1.etat["domaines"])

    def test_nvram_copiee(self):
        self.src.etat["domaines"]["vm1"] = xml_vm("vm1", "lab1", nvram="/var/lib/libvirt/qemu/nvram/vm1_VARS.fd")
        self.lancer()
        self.assertIn(f"{IMAGES}/lab1/vm1_VARS.fd", self.pc1.etat["domaines"]["vm1"])
        self.assertTrue(any("vm1_VARS.fd" in c for c in self.src.commandes(r"rsync -a")))

    def test_nvram_non_copiable_ne_fait_pas_echouer_le_transfert(self):
        self.src.etat["domaines"]["vm1"] = xml_vm("vm1", "lab1", nvram="/var/lib/libvirt/qemu/nvram/vm1_VARS.fd")
        self.src.echecs.append(r"rsync -a .*_VARS")
        ok, ko = self.lancer()
        self.assertEqual((ok, ko), (["pc1"], []))
        self.assertNotIn("nvram", self.pc1.etat["domaines"]["vm1"])

    def test_nom_du_labo_invalide(self):
        with self.assertRaisesRegex(L.Erreur, "nom de labo invalide"):
            self.lancer(nom="../x")

    def test_reseau_inconnu(self):
        with self.assertRaisesRegex(L.Erreur, "absent de src"):
            self.lancer(reseau=["nope"])

    def test_destination_vers_ce_poste_le_poste_tire_les_fichiers(self):
        poste = FauxHote("local", ip="127.0.0.1", utilisateur="prof", local=True)
        lien_pret(poste, self.src)                              # c'est ce poste qui lance rsync
        poste.reponses.append((r"echo labferme-ok", (0, "labferme-ok\n", "")))
        ok, ko = self.lancer(cibles=[poste])
        self.assertEqual((ok, ko), (["local"], []))
        self.assertEqual(self.src.commandes(r"rsync -a"), [], "la source ne pousse rien")
        pull = poste.commandes(r"rsync -a")
        self.assertEqual(len(pull), 2)
        self.assertTrue(pull[0].startswith("sudo -n rsync"))
        self.assertIn("--rsync-path='sudo -n rsync'", pull[0])   # root côté source via sa règle sudo
        self.assertIn("enseignant@10.0.0.1:/var/lib/libvirt/images/vm1.qcow2", pull[0])
        self.assertIn("ssh -i /home/prof/.ssh/labferme", pull[0])
        self.assertEqual(sorted(poste.etat["domaines"]), ["vm1", "vm2"])

    def test_source_ce_poste_vers_un_pc(self):
        poste = machine_avec_labo("local", ip="127.0.0.1", utilisateur="prof", local=True)
        lien_pret(poste, self.pc1)
        ok, ko = L.cmd_transferer(args_transfert(source=poste, cibles=[self.pc1]), None, [])
        self.assertEqual(ko, [])
        push = poste.commandes(r"rsync -a")
        self.assertEqual(len(push), 2)
        self.assertIn("enseignant@10.0.0.2:", push[0])
        self.assertIn("ssh -i /home/prof/.ssh/labferme", push[0])

    def test_ce_poste_vers_ce_poste_impossible(self):
        poste = FauxHote("local", local=True)
        with self.assertRaisesRegex(L.Erreur, "à la fois la source et la destination"):
            L.cmd_transferer(args_transfert(source=poste, cibles=[FauxHote("local", local=True)]), None, [])


class TestChoixDesCibles(unittest.TestCase):
    def setUp(self):
        self.hotes = [FauxHote("pc1", ip="10.0.0.2", pools=["a"]), FauxHote("pc2", ip="10.0.0.3", pools=["a", "b"]),
                      FauxHote("pc3", ip="10.0.0.4", pools=["b"])]
        self.src = self.hotes[0]
        self.autre = FauxHote("portable", ip="10.0.0.9")
        self.autre.source = True

    def noms(self, **kw):
        a = args_transfert(**kw)
        return [L.nom_affiche(h) for h in L.choisir_cibles_transfert(a, self.src, self.hotes, [self.autre])]

    def test_tous_ecarte_la_source(self):
        self.assertEqual(self.noms(tous=True), ["pc2", "pc3"])

    def test_pool(self):
        self.assertEqual(self.noms(pool=["b"]), ["pc2", "pc3"])
        self.assertEqual(self.noms(pool=["a"]), ["pc2"])

    def test_vers_machine_source_et_ce_poste(self):
        self.assertEqual(self.noms(vers=["portable", "local"]), ["portable", "ce poste"])

    def test_pas_de_doublon(self):
        self.assertEqual(self.noms(vers=["pc2"], pool=["b"]), ["pc2", "pc3"])

    def test_nommer_la_source_est_refuse(self):
        with self.assertRaises(L.Erreur):
            self.noms(vers=["pc1"])

    def test_meme_ip_refusee(self):
        with self.assertRaises(L.Erreur):
            L.choisir_cibles_transfert(args_transfert(cibles=[FauxHote("copie", ip="10.0.0.2")]), self.src, self.hotes)

    def test_aucune_destination(self):
        with self.assertRaisesRegex(L.Erreur, "aucune machine de destination"):
            self.noms()

    def test_machine_inconnue(self):
        with self.assertRaisesRegex(L.Erreur, "absente de la configuration"):
            self.noms(vers=["fantome"])


class TestPreparerLiaison(unittest.TestCase):
    def setUp(self):
        self.poste = FauxHote("local", ip="127.0.0.1", utilisateur="prof", local=True)
        self.poste.reponses.append((r"cat /home/prof/.ssh/labferme\.pub", (0, "ssh-ed25519 POSTE labferme-poste\n", "")))
        self.src = FauxHote("src", ip="10.0.0.1")
        self.src.reponses.append((r"cat /home/enseignant/.ssh/labferme\.pub", (0, "ssh-ed25519 SRC labferme-src\n", "")))
        self.pc1 = FauxHote("pc1", ip="10.0.0.2")
        for lanceur in (self.src, self.poste):
            lien_pret(lanceur, self.pc1)
        patch = mock.patch.object(L, "hote_local", return_value=self.poste)
        patch.start()
        self.addCleanup(patch.stop)

    def ajouts(self, h):
        return "".join(e or "" for c, e in h.journal if c.startswith("cat >> ~/.ssh/authorized_keys"))

    def test_cles_autorisees_dans_le_bon_sens(self):
        bilan = L.preparer_liaison(self.src, [self.pc1])
        self.assertIn("POSTE", self.ajouts(self.src))          # ce poste pilote la source
        self.assertIn("POSTE", self.ajouts(self.pc1))          # ... et la cible
        self.assertIn("SRC", self.ajouts(self.pc1))            # la source peut envoyer à la cible
        self.assertNotIn("SRC", self.ajouts(self.src))
        self.assertIn("src peut envoyer vers pc1", bilan)

    def test_aucune_ecriture_sur_un_stockage(self):
        L.preparer_liaison(self.src, [self.pc1])
        self.assertEqual(self.poste.commandes(r"authorized_keys"), [], "le poste ne se modifie pas lui-même")

    def test_ce_poste_comme_destination_ne_recoit_pas_de_cle_distante(self):
        lien_pret(self.poste, self.src)
        L.preparer_liaison(self.src, [self.poste])
        self.assertEqual(self.ajouts(self.poste), "", "ce poste tire les fichiers : sa clé suffit")
        self.assertIn("POSTE", self.ajouts(self.src))

    def test_droits_manquants_declenchent_la_preparation_root(self):
        self.src.reponses.insert(0, (r"sudo -n /usr/bin/virsh --version", (1, "", "sudo: a password is required")))
        appels = []

        def faux_root(h, script, demander_root=None):
            appels.append((h.nom, script))
            self.src.reponses.pop(0)                            # sudo fonctionne après la préparation

        with mock.patch.object(L, "executer_en_root", faux_root):
            L.preparer_liaison(self.src, [self.pc1])
        self.assertEqual([n for n, _ in appels], ["src"])
        self.assertIn("/etc/sudoers.d/labferme-enseignant", appels[0][1])

    def test_echec_de_la_liaison_est_signale(self):
        self.src.reponses.insert(0, (r"echo labferme-ok", (255, "", "Permission denied (publickey)")))
        with self.assertRaisesRegex(L.Erreur, "préparation incomplète"):
            L.preparer_liaison(self.src, [self.pc1])


class TestConfigSansStockage(unittest.TestCase):
    YAML = ("defaut:\n  utilisateur: formateur\n  mot_de_passe: x\nhotes:\n  - nom: pc1\n    ip: 10.0.0.2\n"
            "    pools: [a]\nsources:\n  - nom: portable\n    ip: 10.0.0.9\n")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.chemin = os.path.join(self.dir.name, "ferme.yaml")
        with open(self.chemin, "w", encoding="utf-8") as f:
            f.write(self.YAML)
        os.chmod(self.chemin, 0o600)

    def test_stockage_obligatoire_par_defaut(self):
        with self.assertRaises(L.ConfigIncomplete):
            L.charger_config(self.chemin)

    def test_sans_stockage_accepte(self):
        stockage, hotes = L.charger_config(self.chemin, exiger_stockage=False)
        self.assertIsNone(stockage)
        self.assertEqual([h.nom for h in hotes], ["pc1"])
        self.assertEqual([h.nom for h in L.charger_sources(self.chemin)], ["portable"])

    def test_avec_stockage_inchange(self):
        with open(self.chemin, "a", encoding="utf-8") as f:
            f.write("stockage:\n  ip: 10.0.0.10\n  utilisateur: nas\n  chemin: /srv/labos\n")
        stockage, hotes = L.charger_config(self.chemin)
        self.assertEqual((stockage.ip, stockage.chemin, stockage.chemin_iso), ("10.0.0.10", "/srv/labos",
                                                                              "/srv/labos/iso"))

    def test_commandes_sans_stockage(self):
        self.assertEqual(set(L.SANS_STOCKAGE), {"transferer", "lier", "pc-ajouter"})

    def test_analyse_de_la_ligne_de_commande(self):
        p = L.construire_parser()
        a = p.parse_args(["transferer", "-r", "lab1", "--depuis", "pc1", "--vers", "pc2", "--vers", "local",
                          "--arreter", "--simulation"])
        self.assertEqual((a.commande, a.reseau, a.depuis, a.vers, a.parallele), ("transferer", ["lab1"], "pc1",
                                                                                   ["pc2", "local"], 2))
        a = p.parse_args(["lier", "--depuis", "pc1", "--tous"])
        self.assertTrue(a.tous)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["transferer", "--vers", "pc2"])      # -r obligatoire


if __name__ == "__main__":
    unittest.main()
