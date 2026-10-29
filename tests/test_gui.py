# -*- coding: utf-8 -*-
"""Essai de l'interface (Tkinter) avec des machines simulées derrière Hote.run. Ne s'exécute que s'il y a un
écran : sous un serveur sans écran, utiliser  xvfb-run -a python3 -m unittest discover -s tests -v
"""

import os
import tempfile
import time
import unittest
import unittest.mock

try:
    import tkinter
    tkinter.Tk().destroy()
    ECRAN = True
except Exception:                                   # pas de tkinter, ou pas d'écran
    ECRAN = False

from fakes import FauxHote, L, machine_avec_labo      # noqa: E402

CONFIG = """defaut:
  utilisateur: formateur
  mot_de_passe: x
hotes:
  - nom: KVM-01
    ip: 10.0.0.11
  - nom: KVM-02
    ip: 10.0.0.12
sources:
  - nom: poste-collegue
    ip: 10.0.0.50
    utilisateur: collegue
"""


@unittest.skipUnless(ECRAN, "interface graphique indisponible (pas de tkinter ou pas d'écran)")
class TestInterfaceSansStockage(unittest.TestCase):
    def setUp(self):
        import labferme_gui as G
        from tkinter import messagebox, simpledialog
        self.G = G
        self.dialogues = []
        for nom, rep in (("showinfo", None), ("showwarning", None), ("showerror", None), ("askyesno", True)):
            p = unittest.mock.patch.object(messagebox, nom, lambda *a, _n=nom, _r=rep, **k:
                                           (self.dialogues.append((_n, a[0] if a else "", a[1] if len(a) > 1 else "")), _r)[1])
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(simpledialog, "askstring", lambda *a, **k: "")
        p.start()
        self.addCleanup(p.stop)

        self.src = machine_avec_labo("src", ip="10.0.0.50", utilisateur="collegue", actives=("vm1",))
        self.reg = {"10.0.0.50": self.src,
                    "10.0.0.11": FauxHote("a", ip="10.0.0.11", utilisateur="formateur"),
                    "10.0.0.12": FauxHote("b", ip="10.0.0.12", utilisateur="formateur"),
                    "127.0.0.1": FauxHote("poste", ip="127.0.0.1", utilisateur="prof", local=True)}
        for h in self.reg.values():
            h.etat["existe"].add(f"{h._home}/.ssh/labferme")
            h.reponses.append((r"echo labferme-ok", (0, "labferme-ok\n", "")))
        for nom, valeur in (("run", lambda s, cmd, entree=None, verifier=True: self.reg[s.ip].run(cmd, entree, verifier)),
                            ("fermer", lambda s: None)):
            p = unittest.mock.patch.object(L.Hote, nom, valeur)
            p.start()
            self.addCleanup(p.stop)
        p = unittest.mock.patch.object(L, "sonder", lambda h, stockage=False, delai=4: ("vert", "en ligne", "ok"))
        p.start()
        self.addCleanup(p.stop)
        self.dossier = tempfile.TemporaryDirectory()
        self.addCleanup(self.dossier.cleanup)
        self.config = os.path.join(self.dossier.name, "ferme.yaml")
        with open(self.config, "w", encoding="utf-8") as f:
            f.write(CONFIG)
        os.chmod(self.config, 0o600)

    def piloter(self, scenario):
        """Exécute `scenario` (générateur : chaque yield attend qu'une condition devienne vraie) dans la boucle Tk."""
        app = self.G.Application(self.config)
        erreurs = []
        gen = scenario(app)

        def tick():
            try:
                cond = next(gen)
            except StopIteration:
                app.quit()
                return
            except Exception as e:
                erreurs.append(e)
                app.quit()
                return
            poll(cond, time.time())

        def poll(cond, t0):
            if cond() or time.time() - t0 > 20:
                app.after(80, tick)
            else:
                app.after(50, lambda: poll(cond, t0))
        app.after(300, tick)
        app.after(60000, app.quit)
        app.mainloop()
        for identifiant in app.tk.call("after", "info"):       # évite les messages Tcl des minuteries en attente
            app.after_cancel(identifiant)
        app.destroy()
        if erreurs:
            raise erreurs[0]

    def fenetre(self, app):
        return [w for w in app.winfo_children() if isinstance(w, self.G.FenetreTransfert)][0]

    def test_demarrage_sans_stockage_puis_transfert_vers_deux_pc(self):
        def scenario(app):
            yield lambda: app.hotes and time.time() > 0
            self.assertIsNone(app.stockage)
            self.assertEqual([h.nom for h in app.hotes], ["KVM-01", "KVM-02"])
            self.assertEqual(self.dialogues, [], "aucune fenêtre intempestive quand le fichier n'a pas de stockage")
            app.ouvrir_transfert()
            win = self.fenetre(app)
            yield lambda: win.pret
            win.var_source.set(next(l for l in win.machines if l.startswith("poste-collegue")))
            win.changer_depart()
            yield lambda: win.pret and win.reseaux
            self.assertNotIn("poste-collegue", win.destinations, "la machine de départ n'est pas sa destination")
            win.update()
            x, y, _, _ = win.arbre.bbox("net:lab1")
            win.arbre.event_generate("<Button-1>", x=x + 10, y=y + 5)            # vrai clic sur le réseau
            for iid in ("KVM-01", "KVM-02"):
                yield lambda: True
                x, y, _, _ = win.arbre_dest.bbox(iid)
                win.arbre_dest.event_generate("<Button-1>", x=x + 10, y=y + 5)  # vrai clic sur la destination
            yield lambda: all(win.liaisons.get(i, ("gris",))[0] == "vert" for i in ("KVM-01", "KVM-02"))
            self.assertEqual(win.choisis, {"lab1"})
            self.assertNotIn("disabled", win.btn_transferer.state())
            win.valider()
            yield lambda: not app.occupe and any(d[0] == "showinfo" for d in self.dialogues)
            for ip in ("10.0.0.11", "10.0.0.12"):
                self.assertEqual(sorted(self.reg[ip].etat["domaines"]), ["vm1", "vm2"])
            self.assertIn("vm1", self.src.etat["actifs"], "VM relancée")
        self.piloter(scenario)

    def test_liaison_refusee_bloque_le_transfert(self):
        self.src.reponses.insert(0, (r"echo labferme-ok", (255, "", "Permission denied (publickey)")))

        def scenario(app):
            yield lambda: app.hotes
            app.ouvrir_transfert()
            win = self.fenetre(app)
            yield lambda: win.pret
            win.var_source.set(next(l for l in win.machines if l.startswith("poste-collegue")))
            win.changer_depart()
            yield lambda: win.pret and win.reseaux
            win.choisis = {"lab1"}
            win.cocher({"KVM-01"})
            yield lambda: win.liaisons.get("KVM-01", ("gris",))[0] != "gris"
            self.assertEqual(win.liaisons["KVM-01"][0], "orange")
            self.assertEqual(win.btn_preparer.winfo_manager(), "pack", "bouton « Préparer les liaisons… » proposé")
            self.dialogues.clear()
            win.valider()
            self.assertEqual(self.dialogues[-1][0], "showerror")
            self.assertEqual(self.src.commandes(r"rsync -a|shutdown"), [])
        self.piloter(scenario)

    def test_ajout_et_retrait_d_un_pc_sans_stockage(self):
        def scenario(app):
            yield lambda: app.hotes
            app.ajouter_pc()
            fp = [w for w in app.winfo_children() if isinstance(w, self.G.FenetrePC)][0]
            self.assertFalse(fp.var_preparer.get(), "pas de préparation proposée sans stockage")
            fp.var_nom.set("KVM-03")
            fp.var_ip.set("10.0.0.13")
            fp.var_util.set("formateur")
            fp.enregistrer()
            yield lambda: "KVM-03" in [h.nom for h in app.hotes]
            app.retirer_pc("KVM-03")
            fr = [w for w in app.winfo_children() if isinstance(w, self.G.FenetreRetrait)][0]
            self.assertFalse(fr.var_revoquer.get())
            fr.valider()
            yield lambda: "KVM-03" not in [h.nom for h in app.hotes] and not app.occupe
        self.piloter(scenario)



if __name__ == "__main__":
    unittest.main()
