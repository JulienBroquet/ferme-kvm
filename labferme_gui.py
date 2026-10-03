#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
labferme_gui.py — Ferme KVM 1.0.0 — Interface graphique de labferme.py (même dossier).

    sudo apt install python3-tk python3-paramiko python3-yaml rsync
    python3 labferme_gui.py [-c ferme.yaml]

- Labos du serveur de stockage : lus au démarrage et via « Rafraîchir ».
- Serveurs de la ferme : cocher des PC, charger un pool, enregistrer la sélection comme pool.
- Déployer / retirer le labo choisi sur les PC cochés.
- Exporter un labo de ce poste vers le stockage (choix parmi les réseaux virtuels locaux).
- Voyants : état des PC testé au démarrage puis automatiquement (vert / orange / rouge).
- ISO : bibliothèque d'images ISO sur le stockage, copie vers les PC cochés ou ce poste.
- Journal : fichier ~/.local/state/labferme/labferme.log, consultable via « Journal complet ».
"""

import argparse
import getpass
import os
import queue
import socket
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import labferme as L  # noqa: E402

import yaml  # noqa: E402

COCHE, VIDE = "☑", "☐"
LOCAL = "__local__"            # ligne spéciale « Ce poste » dans la liste des serveurs
ENTETE_CONFIG = ("# Configuration de la ferme — à protéger : chmod 600 ferme.yaml\n"
                 "# Les mots de passe ne servent qu'à « Préparer la ferme » ; ensuite tout passe par clés SSH.\n"
                 "# mot_de_passe_root (facultatif, par PC ou dans defaut) : pour les comptes non sudoers ;\n"
                 "# s'il est absent, l'interface le demande au moment de la préparation.\n"
                 "# Les pools peuvent être modifiés depuis l'interface graphique.\n\n")
COULEURS = {"vert": ("#2ea043", "#1a7f37"), "orange": ("#e3a008", "#9a6700"),
            "rouge": ("#e5534b", "#a40e26"), "gris": ("#b0b0b0", "#808080")}
INTERVALLES = {"30 s": 30, "1 min": 60, "2 min": 120, "5 min": 300}


# Charge CPU/RAM : couleurs d'état (palette de statut, distinctes des séries du graphique)
COULEURS_CHARGE = {"vert": ("#0ca30c", "#087a08"), "orange": ("#fab219", "#b07a00"),
                   "rouge": ("#d03b3b", "#962424"), "gris": ("#d0d0d0", "#a0a0a0")}
SERIE_CPU, SERIE_RAM = "#2a78d6", "#eb6834"      # palette catégorielle validée (bleu, orange)


# Jetons de couleur de l'interface
T = {
    "fond": "#eef1f4",          # espace de travail
    "surface": "#ffffff",       # cartes, listes
    "panneau": "#f5f7f9",       # en-têtes de colonnes, zones secondaires
    "ligne": "#d6dce2",         # bordures fines
    "encre": "#1b2430",         # texte principal
    "doux": "#5f6b7a",          # texte secondaire
    "accent": "#0e6b78",        # actions principales (bleu pétrole)
    "accent_survol": "#0b5c67",
    "accent_fonce": "#084952",
    "accent_clair": "#d6edf0",  # sélection
    "danger": "#b42318",
    "local": "#eef5f7",         # ligne « Ce poste »
    "rayure": "#f8fafb",        # lignes alternées
    "nav": "#1d2833",           # barre latérale (ardoise)
    "nav_survol": "#26333f",
    "nav_actif": "#2e3d4b",
    "nav_ligne": "#33424f",
    "nav_texte": "#c6d1db",
    "nav_doux": "#7f909f",
    "accent_nav": "#3fb3c2",
    "console": "#17212b",
}
CONFLITS = {"arreter": "Ne pas déployer sur ce PC",
            "decaler": "Prendre le sous-réseau libre suivant",
            "sans-ip": "Créer le réseau sans IP (isolé)"}


class Infobulle:
    """Petite aide qui apparaît au survol (texte fixe ou fonction qui renvoie le texte)."""

    def __init__(self, widget, texte, delai=550):
        self.widget, self.texte, self.delai, self.fen, self.minuterie = widget, texte, delai, None, None
        widget.bind("<Enter>", self._prevoir, add="+")
        widget.bind("<Leave>", self._cacher, add="+")
        widget.bind("<ButtonPress>", self._cacher, add="+")

    def _prevoir(self, _):
        self.minuterie = self.widget.after(self.delai, self._montrer)

    def _montrer(self):
        texte = self.texte() if callable(self.texte) else self.texte
        if not texte or self.fen:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.fen = tk.Toplevel(self.widget)
        self.fen.wm_overrideredirect(True)
        self.fen.wm_geometry(f"+{x}+{y}")
        tk.Label(self.fen, text=texte, bg="#1d2833", fg="#ffffff", justify="left", padx=9, pady=6,
                 wraplength=360, font=("TkDefaultFont", 9)).pack()

    def _cacher(self, _=None):
        if self.minuterie:
            self.widget.after_cancel(self.minuterie)
            self.minuterie = None
        if self.fen:
            self.fen.destroy()
            self.fen = None


def arbre_defilant(parent, colonnes, **kw):
    """Treeview + barre de défilement verticale, dans un cadre."""
    cadre = ttk.Frame(parent, style="Carte.TFrame")
    arbre = ttk.Treeview(cadre, columns=colonnes, show="tree headings", **kw)
    sb = ttk.Scrollbar(cadre, command=arbre.yview)
    arbre.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    arbre.pack(side="left", fill="both", expand=True)
    arbre.tag_configure("rayure", background=T["rayure"])
    return cadre, arbre


def aligner_entetes(arbre):
    """En-têtes alignés comme leur colonne (texte à gauche, nombres à droite)."""
    arbre.heading("#0", anchor="w")
    for c in arbre["columns"]:
        arbre.heading(c, anchor=arbre.column(c, "anchor"))


def rayer(arbre):
    """Lignes alternées pour faciliter la lecture des longues listes."""
    for i, iid in enumerate(arbre.get_children()):
        tags = [t for t in arbre.item(iid, "tags") if t != "rayure"]
        if i % 2 and "local" not in tags:
            tags.append("rayure")
        arbre.item(iid, tags=tags)


def _rond(img, couleurs, x0, taille):
    fond, bord = couleurs
    c, r = (taille - 1) / 2, taille / 2 - 1
    for y in range(taille):
        for x in range(taille):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= r:
                img.put(bord if d > r - 1.3 else fond, (x0 + x, y))


def pastille(maitre, couleur, taille=14):
    """Petit voyant rond (image transparente autour)."""
    img = tk.PhotoImage(master=maitre, width=taille, height=taille)
    _rond(img, COULEURS[couleur], 0, taille)
    return img


def double_pastille(maitre, etat, charge, taille=14):
    """Deux voyants côte à côte : état de la connexion + niveau de charge (carré arrondi)."""
    img = tk.PhotoImage(master=maitre, width=2 * taille + 4, height=taille)
    _rond(img, COULEURS[etat], 0, taille)
    _rond(img, COULEURS_CHARGE[charge], taille + 4, taille)
    return img


def texte_charge(v):
    """Barre + valeur ; le symbole double la couleur (jamais la couleur seule)."""
    if v is None:
        return ""
    marque = "‼ " if v >= L.SEUIL_CRITIQUE else "⚠ " if v >= L.SEUIL_ATTENTION else ""
    return f"{marque}{v:.0f} %"


class Application(tk.Tk):
    def __init__(self, chemin_config):
        super().__init__()
        self.title(f"Ferme KVM {L.__version__}")
        l, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(1440, l - 40)}x{min(880, h - 80)}")
        self.minsize(min(1100, l - 40), min(640, h - 80))
        if l < 1500:                                   # petit écran : on prend toute la place
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                pass
        self.chemin_config = os.path.abspath(chemin_config)
        self.stockage, self.hotes = None, []
        self.labos = {}                      # nom -> manifeste
        self.coches = set()                  # noms des serveurs cochés
        self.etats = {}                      # nom serveur -> (couleur, texte, détail, heure)
        self.etat_stockage = ("gris", "", "", "")
        self.occupe = False
        self._sonde_en_cours = False
        self._minuterie = None
        self.fenetre_journal = None
        self._root_commun = None
        self.file_log = queue.Queue()
        L.init_journal()
        L.RAPPEL_LOG = lambda ligne, niveau="info": self.file_log.put((ligne, niveau))
        L.detail("gui", f"démarrage de Ferme KVM {L.__version__}, configuration {self.chemin_config}")
        self.pastilles = {c: pastille(self, c) for c in COULEURS}
        self.doubles = {(e, c): double_pastille(self, e, c) for e in COULEURS for c in COULEURS_CHARGE}
        self.pastilles_charge = {c: pastille(self, "gris") for c in COULEURS_CHARGE}
        for c, img in self.pastilles_charge.items():
            img.blank()
            _rond(img, COULEURS_CHARGE[c], 0, 14)

        self._styles()
        self._construire()
        self.after(100, self._vider_log)
        self.after(200, self.recharger_tout)
        self.protocol("WM_DELETE_WINDOW", self.quitter)

    # ------------------------------------------------------------------ thème

    def _styles(self):
        """Thème « salle serveur » : navigation ardoise, espace de travail clair, accent bleu pétrole."""
        st = ttk.Style(self)
        if "clam" in st.theme_names():
            st.theme_use("clam")
        familles = set(tkfont.families(self))
        famille = next((f for f in ("Inter", "Cantarell", "Noto Sans", "Ubuntu", "Source Sans 3", "DejaVu Sans")
                        if f in familles), "TkDefaultFont")
        for nom in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(nom).configure(family=famille, size=10)
            except tk.TclError:
                pass
        self.police = famille
        self.configure(background=T["fond"])
        self.option_add("*Toplevel.background", T["fond"])
        self.option_add("*Menu.background", T["surface"])
        self.option_add("*Menu.foreground", T["encre"])
        self.option_add("*Menu.activeBackground", T["accent_clair"])
        self.option_add("*Menu.activeForeground", T["encre"])
        self.option_add("*Menu.relief", "flat")

        st.configure(".", background=T["fond"], foreground=T["encre"], bordercolor=T["ligne"],
                     focuscolor=T["accent"], troughcolor=T["fond"], font=(famille, 10))
        st.configure("TFrame", background=T["fond"])
        st.configure("Carte.TFrame", background=T["surface"])
        st.configure("TLabel", background=T["fond"])
        st.configure("Carte.TLabel", background=T["surface"])
        st.configure("Doux.TLabel", foreground=T["doux"])
        st.configure("CarteDoux.TLabel", background=T["surface"], foreground=T["doux"])
        st.configure("Titre.TLabel", font=(famille, 16, "bold"))
        st.configure("Section.TLabel", background=T["surface"], font=(famille, 11, "bold"))
        st.configure("TCheckbutton", background=T["fond"], indicatorbackground=T["surface"],
                     indicatorforeground=T["accent"], upperbordercolor="#aab4be", lowerbordercolor="#aab4be",
                     indicatormargin=(0, 0, 6, 0))
        st.configure("Carte.TCheckbutton", background=T["surface"], indicatorbackground=T["surface"],
                     indicatorforeground=T["accent"], upperbordercolor="#aab4be", lowerbordercolor="#aab4be",
                     indicatormargin=(0, 0, 6, 0))
        st.map("Carte.TCheckbutton", background=[("active", T["surface"])])
        st.map("TCheckbutton", background=[("active", T["fond"])])

        st.configure("TButton", background=T["surface"], foreground=T["encre"], bordercolor="#c3cbd3",
                     lightcolor=T["surface"], darkcolor="#e3e8ec", padding=(12, 6))
        st.map("TButton", background=[("disabled", T["panneau"]), ("pressed", T["ligne"]), ("active", T["panneau"])],
               foreground=[("disabled", "#9aa4af")], bordercolor=[("focus", T["accent"])])
        for nom in ("Primaire.TButton", "Action.TButton"):
            st.configure(nom, background=T["accent"], foreground="#ffffff", bordercolor=T["accent"],
                         lightcolor=T["accent"], darkcolor=T["accent"], font=(famille, 10, "bold"), padding=(14, 7))
            st.map(nom, background=[("disabled", "#9fbcc1"), ("pressed", T["accent_fonce"]),
                                    ("active", T["accent_survol"])],
                   foreground=[("disabled", "#eef4f5")], bordercolor=[("active", T["accent_survol"])])
        st.configure("Danger.TButton", foreground=T["danger"])
        st.map("Danger.TButton", foreground=[("disabled", "#d6a5a0")])
        st.configure("Outil.TButton", padding=(9, 4), width=-3)

        st.configure("Treeview", background=T["surface"], fieldbackground=T["surface"], foreground=T["encre"],
                     bordercolor=T["ligne"], lightcolor=T["surface"], darkcolor=T["surface"], rowheight=28)
        st.map("Treeview", background=[("selected", T["accent_clair"])], foreground=[("selected", T["encre"])])
        st.configure("Treeview.Heading", background=T["panneau"], foreground=T["doux"], relief="flat",
                     bordercolor=T["ligne"], lightcolor=T["panneau"], darkcolor=T["panneau"],
                     font=(famille, 9, "bold"), padding=(6, 5))
        st.map("Treeview.Heading", background=[("active", T["ligne"])])

        for nom in ("TEntry", "TCombobox"):
            st.configure(nom, fieldbackground=T["surface"], bordercolor=T["ligne"], lightcolor=T["surface"],
                         darkcolor=T["surface"], padding=5, arrowcolor=T["doux"])
            st.map(nom, bordercolor=[("focus", T["accent"])], lightcolor=[("focus", T["accent"])],
                   fieldbackground=[("readonly", T["surface"])])
        st.configure("TProgressbar", background=T["accent"], troughcolor=T["ligne"], bordercolor=T["ligne"],
                     lightcolor=T["accent"], darkcolor=T["accent"], thickness=6)
        st.configure("TScrollbar", background=T["panneau"], troughcolor=T["surface"], bordercolor=T["surface"],
                     arrowcolor=T["doux"], lightcolor=T["panneau"], darkcolor=T["panneau"])
        st.configure("TPanedwindow", background=T["fond"])
        st.configure("Sash", sashthickness=8, gripcount=0, background=T["fond"])
        st.configure("TLabelframe", background=T["fond"], bordercolor=T["ligne"])
        st.configure("TLabelframe.Label", background=T["fond"], foreground=T["doux"], font=(famille, 9, "bold"))

    def carte(self, parent, **pack):
        """Bloc blanc à fine bordure : regroupe un ensemble cohérent (liste + ses actions)."""
        c = tk.Frame(parent, bg=T["surface"], highlightthickness=1, highlightbackground=T["ligne"],
                     highlightcolor=T["ligne"])
        if pack:
            c.pack(**pack)
        interieur = ttk.Frame(c, style="Carte.TFrame", padding=12)
        interieur.pack(fill="both", expand=True)
        return interieur

    # ------------------------------------------------------------------ mise en page

    def _construire(self):
        self._navigation()
        principal = ttk.Frame(self)
        principal.pack(side="left", fill="both", expand=True)
        self._barre_etat(principal)
        self.tiroir_journal = self._panneau_log(principal)

        corps = ttk.PanedWindow(principal, orient="horizontal")
        corps.pack(fill="both", expand=True, padx=14, pady=(12, 8))
        # Pages empilées dans la même cellule (grid) : elles imposent leur taille, on affiche celle du dessus.
        self.pile = ttk.Frame(corps, width=640)
        self.pile.rowconfigure(0, weight=1)
        self.pile.columnconfigure(0, weight=1)
        self.pages = {"labos": self._panneau_labos(self.pile), "isos": self._panneau_isos(self.pile)}
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew")
        corps.add(self.pile, weight=5)
        droite = ttk.Frame(corps)
        self._panneau_serveurs(droite)
        corps.add(droite, weight=4)
        self.aller("labos")

        # Répartition gauche / droite : seulement quand la fenêtre a sa vraie largeur (sinon la gauche fait 0 px).
        def repartir(e):
            if e.width > 600 and not getattr(self, "_reparti", False):
                self._reparti = True
                self.after_idle(lambda: corps.sashpos(0, int(e.width * 0.53)))
        corps.bind("<Configure>", repartir, add="+")

        self.bind_all("<F5>", lambda e: self.recharger_tout())
        self.bind_all("<Control-j>", lambda e: self.basculer_journal())
        self.bind_all("<Control-Key-1>", lambda e: self.aller("labos"))
        self.bind_all("<Control-Key-2>", lambda e: self.aller("isos"))
        self.bind_all("<Control-f>", lambda e: self._focus_filtre())

    def _navigation(self):
        nav = tk.Frame(self, bg=T["nav"], width=228)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Label(nav, text="Ferme KVM", bg=T["nav"], fg="#ffffff", font=(self.police, 17, "bold"),
                 anchor="w").pack(fill="x", padx=20, pady=(22, 0))
        tk.Label(nav, text=f"Labos virtuels des ateliers · v{L.__version__}", bg=T["nav"], fg=T["nav_doux"],
                 font=(self.police, 9), anchor="w").pack(fill="x", padx=20, pady=(0, 22))

        self.entrees_nav = {}
        for cle, texte, action, aide in (
                ("labos", "Labos", lambda: self.aller("labos"), "Labos du stockage : déployer, retirer, exporter (Ctrl+1)"),
                ("isos", "Images ISO", lambda: self.aller("isos"), "Bibliothèque d'ISO du stockage (Ctrl+2)"),
                (None, None, None, None),
                ("ferme", "Vue de la ferme", self.ouvrir_vue_ferme, "Tableau : quels labos sur quels PC"),
                ("machine", "Vue machine", lambda: self.ouvrir_vue_machine(None),
                 "VM, réseaux, instantanés et charge d'un PC"),
                ("journal", "Journal complet", self.ouvrir_journal, "Fichier journal avec filtres et recherche")):
            if cle is None:
                tk.Frame(nav, bg=T["nav_ligne"], height=1).pack(fill="x", padx=20, pady=10)
                continue
            self.entrees_nav[cle] = self._entree_nav(nav, texte, action, aide, fenetre=cle not in ("labos", "isos"))

        bas = tk.Frame(nav, bg=T["nav"])
        bas.pack(side="bottom", fill="x", padx=20, pady=18)
        tk.Label(bas, text="Stockage", bg=T["nav"], fg=T["nav_doux"], font=(self.police, 9), anchor="w").pack(fill="x")
        self.lbl_stockage = tk.Label(bas, text="", image=self.pastilles["gris"], compound="left", bg=T["nav"],
                                     fg=T["nav_texte"], anchor="w", justify="left", wraplength=180, cursor="hand2",
                                     font=(self.police, 9), padx=0)
        self.lbl_stockage.pack(fill="x", pady=(2, 12))
        self.lbl_stockage.bind("<Button-1>", lambda e: self.details_stockage())
        Infobulle(self.lbl_stockage, "Cliquer : état du stockage et test d'accès SSH + rsync")
        tk.Label(bas, text="Ferme", bg=T["nav"], fg=T["nav_doux"], font=(self.police, 9), anchor="w").pack(fill="x")
        self.lbl_resume_nav = tk.Label(bas, text="", bg=T["nav"], fg=T["nav_texte"], anchor="w", justify="left",
                                       font=(self.police, 9))
        self.lbl_resume_nav.pack(fill="x", pady=(2, 12))
        tk.Label(bas, text="Configuration", bg=T["nav"], fg=T["nav_doux"], font=(self.police, 9),
                 anchor="w").pack(fill="x")
        self.var_config = tk.StringVar(value=self.chemin_config)
        self.lbl_config = tk.Label(bas, text=os.path.basename(self.chemin_config), bg=T["nav"], fg=T["nav_texte"],
                                   anchor="w", font=(self.police, 9), cursor="hand2")
        self.lbl_config.pack(fill="x", pady=(2, 0))
        self.lbl_config.bind("<Button-1>", lambda e: self.choisir_config())
        Infobulle(self.lbl_config, lambda: f"{self.chemin_config}\nCliquer pour ouvrir un autre fichier")
        for texte, cmd, aide in (("Préparer la ferme…", self.preparer,
                                  "Clés SSH, droits et paquets sur le stockage et les PC (à faire une fois)"),
                                 ("Tout rafraîchir", self.recharger_tout, "Relire la configuration, les labos, "
                                                                          "les ISO et tester les PC (F5)")):
            b = tk.Label(bas, text=texte, bg=T["nav_actif"], fg="#ffffff", font=(self.police, 9, "bold"),
                         pady=7, cursor="hand2")
            b.pack(fill="x", pady=(10, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(bg=T["nav_survol"]))
            b.bind("<Leave>", lambda e, w=b: w.configure(bg=T["nav_actif"]))
            Infobulle(b, aide)

    def _entree_nav(self, nav, texte, action, aide, fenetre=False):
        ligne = tk.Frame(nav, bg=T["nav"], cursor="hand2")
        ligne.pack(fill="x")
        barre = tk.Frame(ligne, bg=T["nav"], width=4)
        barre.pack(side="left", fill="y")
        lbl = tk.Label(ligne, text=texte + ("  ↗" if fenetre else ""), bg=T["nav"], fg=T["nav_texte"],
                       font=(self.police, 11), anchor="w", padx=16, pady=9)
        lbl.pack(side="left", fill="x", expand=True)
        entree = {"ligne": ligne, "barre": barre, "lbl": lbl, "actif": False}

        def peindre(survol=False):
            fond = T["nav_actif"] if entree["actif"] else (T["nav_survol"] if survol else T["nav"])
            for w in (ligne, lbl):
                w.configure(bg=fond)
            barre.configure(bg=T["accent_nav"] if entree["actif"] else fond)
            lbl.configure(fg="#ffffff" if entree["actif"] else T["nav_texte"],
                          font=(self.police, 11, "bold" if entree["actif"] else "normal"))
        entree["peindre"] = peindre
        for w in (ligne, lbl):
            w.bind("<Button-1>", lambda e: action())
            w.bind("<Enter>", lambda e: peindre(True))
            w.bind("<Leave>", lambda e: peindre(False))
        Infobulle(lbl, aide)
        return entree

    def aller(self, page):
        self.page = page
        self.pages[page].tkraise()
        for cle, e in self.entrees_nav.items():
            e["actif"] = cle == page
            e["peindre"]()

    def _focus_filtre(self):
        (self.ent_filtre_labo if self.page == "labos" else self.ent_filtre_iso).focus_set()

    def _champ_recherche(self, parent, variable, indication):
        """Champ de filtre avec texte d'indication grisé quand il est vide."""
        e = ttk.Entry(parent, textvariable=variable, width=26)
        e.indication = indication

        def montrer(*_):
            if not variable.get() and self.focus_get() is not e:
                e.configure(foreground=T["doux"])
                e.delete(0, "end")
                e.insert(0, indication)
                e.vide = True

        def cacher(*_):
            if variable.get() == indication:
                e.delete(0, "end")
                e.configure(foreground=T["encre"])
                e.vide = False
        e.bind("<FocusIn>", cacher)
        e.bind("<FocusOut>", montrer)
        e.vide = False
        self.after(10, montrer)
        return e

    def filtre(self, variable, champ):
        v = variable.get()
        return "" if v == champ.indication else v.strip().lower()

    def _entete_page(self, parent, titre, sous_titre):
        tete = ttk.Frame(parent)
        tete.pack(fill="x", pady=(0, 10))
        ttk.Label(tete, text=titre, style="Titre.TLabel").pack(anchor="w")
        lbl = ttk.Label(tete, text=sous_titre, style="Doux.TLabel", wraplength=640, justify="left")
        lbl.pack(anchor="w")
        return lbl

    # ------------------------------------------------------------------ page Labos

    def _panneau_labos(self, parent):
        page = ttk.Frame(parent)
        self._entete_page(page, "Labos", "Labos enregistrés sur le stockage. Sélectionnez-en un, cochez les PC "
                                         "à droite, puis déployez.")

        actions = ttk.Frame(page)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        self.btn_deployer = ttk.Button(actions, text="Déployer sur les PC cochés", style="Primaire.TButton",
                                       command=self.deployer)
        self.btn_deployer.pack(side="left")
        Infobulle(self.btn_deployer, "Copie le labo sélectionné sur chaque PC coché (place vérifiée avant)")
        b = ttk.Button(actions, text="Retirer…", style="Danger.TButton", command=self.retirer)
        b.pack(side="left", padx=8)
        Infobulle(b, "Supprime les VM, disques et réseaux de ce labo sur les PC cochés")
        b = ttk.Button(actions, text="Exporter depuis ce poste…", command=self.ouvrir_export)
        b.pack(side="right")
        Infobulle(b, "Envoie un labo (réseau + VM) de cette machine vers le stockage")

        opts = self.carte(page, side="bottom", fill="x", pady=(10, 0))
        ttk.Label(opts, text="Options de déploiement", style="Section.TLabel").grid(row=0, column=0, columnspan=2,
                                                                                    sticky="w", pady=(0, 6))
        self.var_remplacer = tk.BooleanVar()
        self.var_remplacer_res = tk.BooleanVar()
        self.var_demarrer = tk.BooleanVar()
        self.var_simulation = tk.BooleanVar()
        for i, (texte, var, aide) in enumerate((
                ("Écraser les VM déjà présentes", self.var_remplacer, "Remplace les VM du même nom (et leurs disques)"),
                ("Redéfinir les réseaux existants", self.var_remplacer_res, "Recrée les réseaux du même nom"),
                ("Démarrer les VM après la copie", self.var_demarrer, "Lance les VM à la fin du déploiement"),
                ("Simulation (ne rien modifier)", self.var_simulation, "Affiche ce qui serait fait, sans rien changer"))):
            c = ttk.Checkbutton(opts, text=texte, variable=var, style="Carte.TCheckbutton")
            c.grid(row=1 + i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=2)
            Infobulle(c, aide)
        ligne = ttk.Frame(opts, style="Carte.TFrame")
        ligne.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(ligne, text="Si le sous-réseau IP est déjà utilisé sur un PC :", style="Carte.TLabel").pack(side="left")
        self.var_conflit = tk.StringVar(value="arreter")
        self.cb_conflit = ttk.Combobox(ligne, state="readonly", width=30, values=list(CONFLITS.values()))
        self.cb_conflit.set(CONFLITS["arreter"])
        self.cb_conflit.pack(side="left", padx=8)
        self.cb_conflit.bind("<<ComboboxSelected>>", lambda e: self._maj_aide_conflit())

        details = self.carte(page, side="bottom", fill="x", pady=(10, 0))
        self.txt_details = tk.Text(details, height=5, wrap="word", relief="flat", background=T["surface"],
                                   foreground=T["encre"], font=(self.police, 10), highlightthickness=0,
                                   padx=2, pady=0)
        self.txt_details.pack(fill="x")
        self.txt_details.tag_configure("titre", font=(self.police, 11, "bold"))
        self.txt_details.tag_configure("doux", foreground=T["doux"])
        self.txt_details.configure(state="disabled")

        liste = self.carte(page, fill="both", expand=True)
        tete = ttk.Frame(liste, style="Carte.TFrame")
        tete.pack(fill="x", pady=(0, 8))
        ttk.Label(tete, text="Labos disponibles", style="Section.TLabel").pack(side="left")
        self.lbl_nb_labos = ttk.Label(tete, text="", style="CarteDoux.TLabel")
        self.lbl_nb_labos.pack(side="left", padx=10)
        b = ttk.Button(tete, text="⟳", width=3, style="Outil.TButton", command=self.rafraichir_labos)
        b.pack(side="right")
        Infobulle(b, "Relire les labos du stockage")
        self.var_filtre_labo = tk.StringVar()
        self.ent_filtre_labo = self._champ_recherche(tete, self.var_filtre_labo, "Rechercher un labo…")
        self.ent_filtre_labo.pack(side="right", padx=8)
        self.var_filtre_labo.trace_add("write", lambda *a: self.afficher_labos(None))

        cols = ("proprietaire", "vms", "taille", "date", "reseaux")
        cadre, self.arbre_labos = arbre_defilant(liste, cols, selectmode="browse")
        self.arbre_labos.heading("#0", text="Labo")
        self.arbre_labos.column("#0", width=170)
        for c, t, w in zip(cols, ("Propriétaire", "VM", "Taille", "Exporté le", "Réseaux"), (95, 40, 75, 140, 120)):
            self.arbre_labos.heading(c, text=t)
            self.arbre_labos.column(c, width=w, anchor="e" if c in ("vms", "taille") else "w",
                                    stretch=c == "reseaux")
        self.arbre_labos.bind("<<TreeviewSelect>>", lambda e: (self.afficher_details(), self.afficher_serveurs()))
        aligner_entetes(self.arbre_labos)
        cadre.pack(fill="both", expand=True)
        self._labos_tous = []
        return page

    # ------------------------------------------------------------------ page ISO

    def _panneau_isos(self, parent):
        page = ttk.Frame(parent)
        self._entete_page(page, "Images ISO", "Bibliothèque partagée sur le stockage. Les ISO copiées sur un PC "
                                              f"apparaissent dans virt-manager (pool « {L.POOL_ISO} »).")
        actions = ttk.Frame(page)
        actions.pack(side="bottom", fill="x", pady=(10, 0))
        b = ttk.Button(actions, text="Copier sur les PC cochés", style="Primaire.TButton", command=self.copier_isos_pc)
        b.pack(side="left")
        Infobulle(b, "Chaque PC coché télécharge les ISO sélectionnées depuis le stockage")
        b = ttk.Button(actions, text="Sur ce poste", command=self.copier_isos_local)
        b.pack(side="left", padx=8)
        b = ttk.Button(actions, text="Supprimer…", style="Danger.TButton", command=self.supprimer_isos)
        b.pack(side="right")
        b = ttk.Button(actions, text="Ajouter…", command=self.envoyer_isos)
        b.pack(side="right", padx=8)
        Infobulle(b, "Envoyer des fichiers .iso de cette machine vers le stockage")

        liste = self.carte(page, fill="both", expand=True)
        tete = ttk.Frame(liste, style="Carte.TFrame")
        tete.pack(fill="x")
        ttk.Label(tete, text="ISO disponibles", style="Section.TLabel").pack(side="left")
        self.lbl_nb_iso = ttk.Label(tete, text="", style="CarteDoux.TLabel")
        self.lbl_nb_iso.pack(side="left", padx=10)
        b = ttk.Button(tete, text="⟳", width=3, style="Outil.TButton", command=self.rafraichir_isos)
        b.pack(side="right")
        Infobulle(b, "Relire les ISO du stockage")
        self.var_filtre_iso = tk.StringVar()
        self.ent_filtre_iso = self._champ_recherche(tete, self.var_filtre_iso, "Rechercher une ISO…")
        self.ent_filtre_iso.pack(side="right", padx=8)
        self.var_filtre_iso.trace_add("write", lambda *a: self.afficher_isos())

        emplacement = ttk.Frame(liste, style="Carte.TFrame")
        emplacement.pack(fill="x", pady=(4, 8))
        b = ttk.Button(emplacement, text="Changer l'emplacement…", style="Outil.TButton",
                       command=self.changer_dossier_iso)
        b.pack(side="right")
        self.lbl_dossier_iso = ttk.Label(emplacement, text="", style="CarteDoux.TLabel")
        self.lbl_dossier_iso.pack(side="left", fill="x", expand=True)
        Infobulle(self.lbl_dossier_iso, lambda: self.stockage and f"{self.stockage.ip}:{self.stockage.chemin_iso}")

        cols = ("taille", "date")
        cadre, self.arbre_isos = arbre_defilant(liste, cols, selectmode="extended")
        self.arbre_isos.heading("#0", text="Fichier")
        self.arbre_isos.column("#0", width=360)
        self.arbre_isos.heading("taille", text="Taille")
        self.arbre_isos.column("taille", width=90, anchor="e")
        self.arbre_isos.heading("date", text="Ajoutée le")
        self.arbre_isos.column("date", width=140)
        aligner_entetes(self.arbre_isos)
        cadre.pack(fill="both", expand=True)
        ttk.Label(liste, text="Ctrl + clic pour sélectionner plusieurs ISO.", style="CarteDoux.TLabel").pack(
            anchor="w", pady=(6, 0))
        self.isos = []
        return page

    # ------------------------------------------------------------------ serveurs (toujours visibles)

    def _panneau_serveurs(self, parent):
        ttk.Frame(parent, height=58).pack(fill="x")           # aligne la carte sur celles de la page
        f = self.carte(parent, fill="both", expand=True)
        tete = ttk.Frame(f, style="Carte.TFrame")
        tete.pack(fill="x")
        ttk.Label(tete, text="PC de la ferme", style="Section.TLabel").pack(side="left")
        self.lbl_coches = ttk.Label(tete, text="", style="CarteDoux.TLabel")
        self.lbl_coches.pack(side="right")

        outils = ttk.Frame(f, style="Carte.TFrame")
        outils.pack(fill="x", pady=(10, 6))
        for texte, cmd, aide in (
                ("Tous", lambda: self._cocher(set(h.nom for h in self.hotes)), "Cocher tous les PC"),
                ("Aucun", lambda: self._cocher(set()), "Tout décocher"),
                ("En ligne", lambda: self._cocher({h.nom for h in self.hotes
                                                   if self.etats.get(h.nom, ("gris",))[0] == "vert"}),
                 "Cocher uniquement les PC prêts (voyant vert)")):
            b = ttk.Button(outils, text=texte, style="Outil.TButton", command=cmd)
            b.pack(side="left", padx=(0, 4))
            Infobulle(b, aide)
        ttk.Label(outils, text="Pool", style="Carte.TLabel").pack(side="left", padx=(14, 6))
        self.var_pool = tk.StringVar()
        self.cb_pool = ttk.Combobox(outils, textvariable=self.var_pool, state="readonly", width=14)
        self.cb_pool.pack(side="left")
        self.cb_pool.bind("<<ComboboxSelected>>", lambda e: self.cocher_pool())
        Infobulle(self.cb_pool, "Choisir un pool coche ses PC")
        b = ttk.Button(outils, text="⋯", width=3, style="Outil.TButton")
        b.pack(side="left", padx=4)
        menu_pool = tk.Menu(self, tearoff=0)
        menu_pool.add_command(label="Enregistrer les PC cochés comme pool…", command=self.enregistrer_pool)
        menu_pool.add_command(label="Supprimer ce pool", command=self.supprimer_pool)
        b.configure(command=lambda w=b: menu_pool.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height()))
        Infobulle(b, "Gérer les pools")
        b = ttk.Button(outils, text="⟳", style="Outil.TButton", command=lambda: self.surveiller(manuel=True))
        b.pack(side="right")
        Infobulle(b, "Tester maintenant la connexion et la charge de tous les PC")

        cols = ("ip", "cpu", "ram", "libre", "etat")
        cadre, self.arbre_srv = arbre_defilant(f, cols, selectmode="none")
        self.arbre_srv.heading("#0", text="PC")
        self.arbre_srv.column("#0", width=150, stretch=False)
        for c, t, w in zip(cols, ("Adresse IP", "CPU", "RAM", "Libre", "État"), (100, 54, 54, 70, 150)):
            self.arbre_srv.heading(c, text=t)
            self.arbre_srv.column(c, width=w, anchor="e" if c in ("libre", "cpu", "ram") else "w",
                                  stretch=c == "etat")
        aligner_entetes(self.arbre_srv)
        self.arbre_srv.tag_configure("rouge", foreground=T["danger"])
        self.arbre_srv.tag_configure("orange", foreground="#8a5a00")
        self.arbre_srv.tag_configure("local", background=T["local"])
        cadre.pack(fill="both", expand=True)
        self.arbre_srv.bind("<Button-1>", self._clic_serveur)
        self.arbre_srv.bind("<Button-3>", self._menu_serveur)
        self.arbre_srv.bind("<Double-Button-1>", self._double_clic_serveur)
        self.menu_srv = tk.Menu(self, tearoff=0)

        legende = ttk.Frame(f, style="Carte.TFrame")
        legende.pack(fill="x", pady=(8, 0))
        for c, t in (("vert", "prêt"), ("orange", "à préparer"), ("rouge", "hors ligne")):
            ttk.Label(legende, image=self.pastilles[c], text=" " + t, compound="left",
                      style="CarteDoux.TLabel").pack(side="left", padx=(0, 10))
        ttk.Label(legende, text="·  charge", style="CarteDoux.TLabel").pack(side="left", padx=(4, 8))
        for c, t in (("vert", "ok"), ("orange", "⚠"), ("rouge", "‼")):
            ttk.Label(legende, image=self.pastilles_charge[c], text=" " + t, compound="left",
                      style="CarteDoux.TLabel").pack(side="left", padx=(0, 8))
        Infobulle(legende, f"1er voyant : connexion du PC.\n2e voyant : charge (le plus élevé du CPU et de la "
                           f"RAM) : ok < {L.SEUIL_ATTENTION} %, ⚠ ≥ {L.SEUIL_ATTENTION} %, ‼ ≥ {L.SEUIL_CRITIQUE} %."
                           f"\nDouble-clic sur un PC : Vue machine. Clic droit : autres actions.")


        auto = ttk.Frame(f, style="Carte.TFrame")
        auto.pack(fill="x", pady=(6, 0))
        self.var_auto = tk.BooleanVar(value=True)
        self.var_intervalle = tk.StringVar(value="1 min")
        ttk.Checkbutton(auto, text="Tester automatiquement toutes les", variable=self.var_auto,
                        style="Carte.TCheckbutton", command=self.programmer_surveillance).pack(side="left")
        cb = ttk.Combobox(auto, textvariable=self.var_intervalle, values=list(INTERVALLES), width=6, state="readonly")
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.programmer_surveillance())
        self.lbl_derniere = ttk.Label(auto, text="", style="CarteDoux.TLabel")
        self.lbl_derniere.pack(side="right")
        return f

    # ------------------------------------------------------------------ journal (tiroir) et barre d'état

    def _panneau_log(self, parent):
        tiroir = tk.Frame(parent, bg=T["console"], height=190)
        tiroir.pack_propagate(False)
        tete = tk.Frame(tiroir, bg=T["console"])
        tete.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(tete, text="Activité", bg=T["console"], fg="#ffffff", font=(self.police, 10, "bold")).pack(side="left")
        for texte, cmd in (("Masquer", self.basculer_journal), ("Effacer", self.effacer_log),
                           ("Journal complet ↗", self.ouvrir_journal)):
            b = tk.Label(tete, text=texte, bg=T["console"], fg="#9fb3c8", cursor="hand2", font=(self.police, 9))
            b.pack(side="right", padx=(12, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
        cadre = tk.Frame(tiroir, bg=T["console"])
        cadre.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.txt_log = tk.Text(cadre, wrap="word", font=("TkFixedFont", 9), relief="flat", highlightthickness=0,
                               background=T["console"], foreground="#d5dde5", insertbackground="white")
        sb = ttk.Scrollbar(cadre, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.tag_configure("erreur", foreground="#ff8a80")
        self.txt_log.tag_configure("alerte", foreground="#f2c14e")
        self.txt_log.tag_configure("succes", foreground="#7ee2a8")
        self.txt_log.tag_configure("heure", foreground="#7d8b99")
        self.journal_visible = False
        return tiroir

    def _barre_etat(self, parent):
        barre = tk.Frame(parent, bg=T["surface"], highlightthickness=1, highlightbackground=T["ligne"])
        barre.pack(side="bottom", fill="x")
        interieur = tk.Frame(barre, bg=T["surface"])
        interieur.pack(fill="x", padx=14, pady=6)
        self.lbl_statut = tk.Label(interieur, text="Prêt", bg=T["surface"], fg=T["encre"],
                                   font=(self.police, 9, "bold"))
        self.lbl_statut.pack(side="left")
        self.barre = ttk.Progressbar(interieur, mode="indeterminate", length=140)   # visible seulement si occupé
        self.lbl_message = tk.Label(interieur, text="", bg=T["surface"], fg=T["doux"], anchor="w",
                                    font=(self.police, 9))
        self.lbl_message.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.btn_journal = tk.Label(interieur, text="Activité ▴", bg=T["panneau"], fg=T["encre"], cursor="hand2",
                                    font=(self.police, 9, "bold"), padx=10, pady=3)
        self.btn_journal.pack(side="right")
        self.btn_journal.bind("<Button-1>", lambda e: self.basculer_journal())
        Infobulle(self.btn_journal, "Afficher l'activité récente (Ctrl+J)")
        self.nb_erreurs = 0

    def basculer_journal(self):
        self.journal_visible = not self.journal_visible
        if self.journal_visible:
            self.tiroir_journal.pack(side="bottom", fill="x")
            self.nb_erreurs = 0
            self.txt_log.see("end")
        else:
            self.tiroir_journal.pack_forget()
        self._maj_bouton_journal()

    def _maj_bouton_journal(self):
        fleche = "▾" if self.journal_visible else "▴"
        if self.nb_erreurs and not self.journal_visible:
            self.btn_journal.configure(text=f"Activité {fleche}  ·  {self.nb_erreurs} erreur(s)",
                                       bg="#fde8e6", fg=T["danger"])
        else:
            self.btn_journal.configure(text=f"Activité {fleche}", bg=T["panneau"], fg=T["encre"])

    def _maj_aide_conflit(self):
        cle = next(k for k, v in CONFLITS.items() if v == self.cb_conflit.get())
        self.var_conflit.set(cle)

    # ------------------------------------------------------------------ utilitaires

    def ecrire_log(self, ligne, niveau="info"):
        couleur = {"erreur": T["danger"], "alerte": "#8a5a00", "succes": "#1a7f37"}.get(niveau, T["doux"])
        self.lbl_message.configure(text=ligne.splitlines()[0][:160], fg=couleur)
        if niveau == "erreur" and not self.journal_visible:
            self.nb_erreurs += 1
            self._maj_bouton_journal()
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", time.strftime("%H:%M:%S "), "heure")
        self.txt_log.insert("end", ligne + "\n", niveau)
        if int(self.txt_log.index("end-1c").split(".")[0]) > 3000:
            self.txt_log.delete("1.0", "500.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def effacer_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _vider_log(self):
        try:
            while True:
                self.ecrire_log(*self.file_log.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._vider_log)

    def ouvrir_journal(self):
        if self.fenetre_journal and self.fenetre_journal.winfo_exists():
            self.fenetre_journal.deiconify()
            self.fenetre_journal.lift()
        else:
            self.fenetre_journal = FenetreJournal(self)

    def ouvrir_vue_ferme(self):
        if getattr(self, "fenetre_ferme", None) and self.fenetre_ferme.winfo_exists():
            self.fenetre_ferme.deiconify()
            self.fenetre_ferme.lift()
            self.fenetre_ferme.actualiser()
        else:
            self.fenetre_ferme = FenetreFerme(self)

    def ouvrir_vue_machine(self, nom):
        """nom : nom d'un PC, LOCAL pour ce poste, ou None (premier PC en ligne)."""
        if nom is None:
            nom = next((h.nom for h in self.hotes if self.etats.get(h.nom, ("gris",))[0] == "vert"), LOCAL)
        FenetreMachine(self, nom)

    def hote_par_nom(self, nom):
        return L.hote_local() if nom == LOCAL else next(h for h in self.hotes if h.nom == nom)

    def selectionner_labo_et_pc(self, labo, noms_pc):
        """Depuis la vue de la ferme : sélectionne le labo et coche les PC où il est déployé."""
        self.aller("labos")
        if self.arbre_labos.exists(labo):
            self.arbre_labos.selection_set(labo)
            self.arbre_labos.see(labo)
        self._cocher(set(noms_pc))
        self.lift()

    def quitter(self):
        L.detail("gui", "fermeture de l'interface")
        self.destroy()

    def tache(self, titre, fonction, apres=None):
        """Exécute fonction() dans un thread ; apres(résultat) est appelé dans l'interface."""
        if self.occupe:
            messagebox.showinfo("Patience", "Une opération est déjà en cours.")
            return
        self.occupe = True
        self.lbl_statut.configure(text=titre + "…")
        self.barre.pack(side="left", padx=(10, 0), after=self.lbl_statut)
        self.barre.start(12)
        self.configure(cursor="watch")

        L.detail("gui", f"début : {titre}")

        def travail():
            try:
                res, err = fonction(), None
            except Exception as e:
                res, err = None, e
                L.detail("gui", f"{titre} :\n{traceback.format_exc()}")
            self.after(0, lambda: fin(res, err))

        def fin(res, err):
            self.occupe = False
            self.barre.stop()
            self.barre.pack_forget()
            self.configure(cursor="")
            self.lbl_statut.configure(text="Prêt")
            if err:
                L.log("ERREUR", f"{titre} : {err}")
                messagebox.showerror(titre, f"{err}\n\nDétails : bouton « Journal complet ».")
            elif apres:
                apres(res)

        threading.Thread(target=travail, daemon=True).start()

    def pc_coches(self):
        return [h for h in self.hotes if h.nom in self.coches]

    def labo_choisi(self):
        sel = self.arbre_labos.selection()
        return self.labos.get(sel[0]) if sel else None

    # ------------------------------------------------------------------ configuration & chargements

    def choisir_config(self):
        f = filedialog.askopenfilename(title="Fichier de configuration",
                                       initialdir=os.path.dirname(self.chemin_config),
                                       filetypes=[("YAML", "*.yaml *.yml"), ("Tous", "*")])
        if f:
            self.chemin_config = f
            self.var_config.set(f)
            self.recharger_tout()

    def charger_config(self):
        if self.stockage:
            self.stockage.fermer()
        self.stockage, self.hotes = L.charger_config(self.chemin_config)
        noms = {h.nom for h in self.hotes}
        self.coches &= noms
        self.etats = {n: e for n, e in self.etats.items() if n in noms or n == LOCAL}
        court = "/".join(self.stockage.chemin.rstrip("/").split("/")[-2:])
        self.lbl_stockage.configure(text=f" {self.stockage.ip}\n …/{court}")
        self.lbl_config.configure(text=os.path.basename(self.chemin_config))
        self.lbl_dossier_iso.configure(text=f"Dossier : {self.stockage.chemin_iso}")
        self.afficher_serveurs()

    def recharger_tout(self):
        try:
            self.charger_config()
        except Exception as e:
            L.log("ERREUR", f"configuration : {e}")
            messagebox.showerror("Configuration", str(e))
            return
        self.surveiller()
        self.rafraichir_labos()
        self.programmer_surveillance()

    def rafraichir_labos(self):
        if not self.stockage:
            return
        self.tache("Lecture du stockage", lambda: (L.lister_labos(self.stockage), self._lire_isos()),
                   lambda r: (self.afficher_labos(r[0]), self._afficher_isos_lus(r[1])))

    def afficher_labos(self, labos):
        """labos = nouvelle liste lue sur le stockage, ou None pour seulement réappliquer le filtre."""
        if labos is not None:
            self._labos_tous = labos
            self.labos = {m["nom"]: m for m in labos}
            L.log("stockage", f"{len(labos)} labo(s) disponible(s)")
        ancien = self.arbre_labos.selection()
        self.arbre_labos.delete(*self.arbre_labos.get_children())
        filtre = self.filtre(self.var_filtre_labo, self.ent_filtre_labo)
        visibles = [m for m in self._labos_tous if not filtre or filtre in
                    f"{m['nom']} {m.get('proprietaire', '')} {m.get('description', '')}".lower()]
        for m in visibles:
            self.arbre_labos.insert("", "end", iid=m["nom"], text=f"  {m['nom']}", values=(
                m.get("proprietaire", "?"), len(m.get("vms", [])), m.get("taille", ""),
                m.get("date", ""), ", ".join(r["nom"] for r in m.get("reseaux", []))))
        rayer(self.arbre_labos)
        if ancien and self.arbre_labos.exists(ancien[0]):
            self.arbre_labos.selection_set(ancien[0])
        n = len(self._labos_tous)
        self.lbl_nb_labos.configure(text=f"{len(visibles)} sur {n}" if filtre else f"{n} labo(s)")
        self.afficher_details()

    def afficher_details(self):
        m = self.labo_choisi()
        t = self.txt_details
        t.configure(state="normal")
        t.delete("1.0", "end")
        if not m:
            t.insert("end", "Sélectionnez un labo pour voir son contenu et la place nécessaire.", "doux")
        else:
            t.insert("end", m["nom"], "titre")
            t.insert("end", f"   {m.get('description') or 'sans description'}\n", "doux")
            t.insert("end", f"Exporté par {m.get('proprietaire', '?')} depuis {m.get('source', '?')}, "
                            f"le {m.get('date', '?')}\n", "doux")
            t.insert("end", "Réseaux : " + (", ".join(f"{r['nom']}" for r in m.get("reseaux", [])) or "-") + "\n")
            t.insert("end", "VM : " + (", ".join(f"{v['nom']} ({len(v['disques'])} disque)"
                                                 for v in m.get("vms", [])) or "-") + "\n")
            if m.get("octets"):
                t.insert("end", f"Place nécessaire sur chaque PC : {L.taille_lisible(m['octets'])} "
                                f"(+ {L.taille_lisible(L.MARGE_DISQUE)} de marge). "
                                f"Un ⚠ dans la colonne « Libre » signale un PC trop plein.", "doux")
        t.configure(state="disabled")

    # ------------------------------------------------------------------ images ISO

    def _lire_isos(self):
        try:
            return L.lister_isos(self.stockage)
        except Exception as e:
            L.log("stockage", f"lecture des ISO impossible : {e}", "erreur")
            return None

    def _afficher_isos_lus(self, isos):
        if isos is not None:
            self.isos = isos
            self.afficher_isos()

    def rafraichir_isos(self):
        if self.stockage:
            self.tache("Lecture des ISO", lambda: L.lister_isos(self.stockage),
                       lambda isos: (self._afficher_isos_lus(isos),
                                     L.log("stockage", f"{len(isos)} ISO disponible(s)")))

    def afficher_isos(self):
        anciens = set(self.arbre_isos.selection())
        self.arbre_isos.delete(*self.arbre_isos.get_children())
        filtre = self.filtre(self.var_filtre_iso, self.ent_filtre_iso)
        n, total = 0, 0
        for i in self.isos:
            if filtre and filtre not in i["nom"].lower():
                continue
            self.arbre_isos.insert("", "end", iid=i["nom"], text=f"  {i['nom']}",
                                   values=(L.taille_lisible(i["taille"]), i["date"]))
            n += 1
            total += i["taille"]
        rayer(self.arbre_isos)
        self.arbre_isos.selection_set([x for x in anciens if self.arbre_isos.exists(x)])
        self.lbl_nb_iso.configure(text=f"{n} ISO, {L.taille_lisible(total)}")

    def isos_choisies(self):
        noms = list(self.arbre_isos.selection())
        if not noms:
            messagebox.showinfo("ISO", "Sélectionnez une ou plusieurs ISO dans la liste.")
        return noms

    def changer_dossier_iso(self):
        if not self.stockage:
            return
        chemin = simpledialog.askstring(
            "Emplacement des ISO", "Dossier des ISO sur le serveur de stockage\n"
                                   "(chemin complet, il sera créé s'il n'existe pas) :",
            initialvalue=self.stockage.chemin_iso, parent=self)
        if not chemin or chemin.strip().rstrip("/") == self.stockage.chemin_iso:
            return
        chemin = chemin.strip()

        def fin(chemin_ok):
            self._modifier_config(lambda cfg: cfg["stockage"].__setitem__("chemin_iso", chemin_ok))
            L.log("config", f"emplacement des ISO : {chemin_ok}", "succes")
            self.rafraichir_isos()

        self.tache("Vérification du dossier ISO", lambda: L.verifier_dossier_iso(self.stockage, chemin), fin)

    def envoyer_isos(self):
        fichiers = filedialog.askopenfilenames(title="ISO à envoyer sur le stockage",
                                               filetypes=[("Images ISO", "*.iso *.ISO"), ("Tous", "*")])
        if not fichiers:
            return
        existants = {i["nom"] for i in self.isos}
        doublons = [os.path.basename(f) for f in fichiers if os.path.basename(f) in existants]
        if doublons and not messagebox.askyesno("ISO", f"Déjà sur le stockage : {', '.join(doublons)}\n"
                                                       "Les remplacer ?"):
            fichiers = [f for f in fichiers if os.path.basename(f) not in existants]
            if not fichiers:
                return

        def travail():
            for f in fichiers:
                L.envoyer_iso(self.stockage, f)
            return L.lister_isos(self.stockage)

        self.tache(f"Envoi de {len(fichiers)} ISO", travail, self._afficher_isos_lus)

    def _copier_isos(self, noms, hotes_choisis, local):
        args = argparse.Namespace(tous=False, pool=None, hote=[h.nom for h in hotes_choisis] or None,
                                  local=local, parallele=4)

        def travail():
            echecs = []
            for nom in noms:
                args.nom = nom
                ok, ko = L.cmd_iso_copier(args, self.stockage, self.hotes)
                echecs += [f"{nom} → {x}" for x in ko]
            return echecs

        def fin(echecs):
            if echecs:
                messagebox.showwarning("ISO", "Échecs :\n" + "\n".join(echecs) + "\n\nVoir le journal.")
            else:
                messagebox.showinfo("ISO", "Copie terminée. Dans virt-manager, choisissez l'ISO dans le pool "
                                           f"« {L.POOL_ISO} » lors de la création de la machine.")

        dest = "ce poste" if local else f"{len(hotes_choisis)} PC"
        self.tache(f"Copie de {len(noms)} ISO vers {dest}", travail, fin)

    def copier_isos_pc(self):
        noms = self.isos_choisies()
        if not noms:
            return
        cibles = self.pc_coches()
        if not cibles:
            messagebox.showinfo("ISO", "Cochez les PC de destination (à droite).")
            return
        hors = [h.nom for h in cibles if self.etats.get(h.nom, ("gris",))[0] != "vert"]
        if hors and not messagebox.askyesno("ISO", f"PC pas prêts (voyant non vert) : {', '.join(hors)}.\n"
                                                   "Continuer quand même ?", icon="warning"):
            return
        if messagebox.askyesno("ISO", f"Copier {len(noms)} ISO sur {len(cibles)} PC :\n"
                                      f"{', '.join(h.nom for h in cibles)} ?"):
            self._copier_isos(noms, cibles, False)

    def copier_isos_local(self):
        noms = self.isos_choisies()
        if noms:
            self.assurer_droits_locaux(lambda: self._copier_isos(noms, [], True))

    def supprimer_isos(self):
        noms = self.isos_choisies()
        if not noms or not messagebox.askyesno(
                "ISO", f"Supprimer définitivement du stockage :\n{chr(10).join(noms)} ?", icon="warning"):
            return

        def travail():
            for n in noms:
                L.supprimer_iso(self.stockage, n)
            return L.lister_isos(self.stockage)

        self.tache("Suppression d'ISO", travail, self._afficher_isos_lus)

    # ------------------------------------------------------------------ serveurs & pools

    def besoin_labo(self):
        """Octets nécessaires sur chaque PC pour le labo sélectionné (None si aucun)."""
        m = self.labo_choisi()
        return m.get("octets") if m else None

    def texte_libre(self, nom):
        libre = L.ESPACES.get(nom)
        if libre is None:
            return "?"
        besoin = self.besoin_labo()
        manque = nom != "local" and besoin and libre < besoin + L.MARGE_DISQUE
        return ("⚠ " if manque else "") + L.taille_lisible(libre)

    def _mesure_valide(self, nom):
        """Dernière mesure de charge, si elle est récente (< 3 intervalles de surveillance)."""
        m = L.CHARGES.get(nom)
        limite = 3 * INTERVALLES.get(self.var_intervalle.get(), 60) + 30
        etat = self.etats.get(LOCAL if nom == "local" else nom, ("gris",))[0]
        return m if m and etat != "rouge" and time.time() - m["heure"] < limite else None

    def _image_pc(self, nom, etat):
        m = self._mesure_valide(nom)
        return self.doubles[(etat, L.niveau_charge(m) if m else "gris")]

    def _charges_txt(self, nom):
        m = self._mesure_valide(nom)
        return (texte_charge(m["cpu"]), texte_charge(m["ram"])) if m else ("", "")

    def afficher_serveurs(self):
        self.arbre_srv.delete(*self.arbre_srv.get_children())
        couleur, texte, _, _ = self.etats.get(LOCAL, ("gris", "pas encore testé", "", ""))
        self.arbre_srv.insert("", "end", iid=LOCAL, tags=(couleur, "local"), image=self._image_pc("local", couleur),
                              text="   Ce poste", values=("local", *self._charges_txt("local"),
                                                          self.texte_libre("local"), texte))
        for h in self.hotes:
            couleur, texte, _, _ = self.etats.get(h.nom, ("gris", "pas encore testé", "", ""))
            n_vm = re.search(r"(\d+) VM", texte)                 # voyant vert = en ligne : on garde l'utile
            if couleur == "vert" and n_vm:
                texte = f"prêt, {n_vm.group(1)} VM"
            self.arbre_srv.insert("", "end", iid=h.nom, tags=(couleur,), image=self._image_pc(h.nom, couleur),
                                  text=f" {COCHE if h.nom in self.coches else VIDE}  {h.nom}",
                                  values=(h.ip, *self._charges_txt(h.nom), self.texte_libre(h.nom), texte))
        rayer(self.arbre_srv)
        nb = {c: sum(1 for h in self.hotes if self.etats.get(h.nom, ("gris",))[0] == c) for c in COULEURS}
        pools = sorted({p for h in self.hotes for p in h.pools})
        self.cb_pool.configure(values=pools)
        if self.var_pool.get() not in pools:
            self.var_pool.set("")
        resume = f"{nb['vert']} en ligne"
        if nb["orange"]:
            resume += f", {nb['orange']} à préparer"
        if nb["rouge"]:
            resume += f", {nb['rouge']} hors ligne"
        self.lbl_coches.configure(text=f"{len(self.coches)} coché(s) sur {len(self.hotes)}")
        self.lbl_resume_nav.configure(text=resume.replace(", ", "\n"))

    def _clic_serveur(self, event):
        iid = self.arbre_srv.identify_row(event.y)
        if iid == LOCAL:
            self.lbl_statut.configure(text="« Ce poste » ne peut pas être coché : clic droit pour le préparer")
        elif iid:
            self.coches ^= {iid}
            self.afficher_serveurs()

    def _double_clic_serveur(self, event):
        iid = self.arbre_srv.identify_row(event.y)
        if iid:
            if iid != LOCAL:                     # annule la (dé)coche du premier clic
                self.coches ^= {iid}
                self.afficher_serveurs()
            self.ouvrir_vue_machine(iid)
        return "break"

    def _menu_serveur(self, event):
        iid = self.arbre_srv.identify_row(event.y)
        if not iid:
            return
        m = self.menu_srv
        m.delete(0, "end")
        if iid == LOCAL:
            m.add_command(label="🖥 Vue machine (réseaux, VM, instantanés)…",
                          command=lambda: self.ouvrir_vue_machine(LOCAL))
            m.add_command(label="Détails de l'état de ce poste…", command=lambda: self.details_serveur(LOCAL))
            m.add_command(label="Tester ce poste", command=lambda: self.surveiller(manuel=True, noms=[LOCAL]))
            m.add_command(label="Tester l'accès au stockage (SSH + rsync)",
                          command=lambda: self.tester_acces(L.hote_local(), "Ce poste"))
            m.add_separator()
            m.add_command(label="Préparer ce poste…", command=self.preparer_local)
            m.tk_popup(event.x_root, event.y_root)
            return
        m.add_command(label=f"🖥 Vue machine de {iid} (réseaux, VM, instantanés)…",
                      command=lambda: self.ouvrir_vue_machine(iid))
        m.add_command(label=f"Détails de l'état de {iid}…", command=lambda: self.details_serveur(iid))
        m.add_command(label="Tester ce PC", command=lambda: self.surveiller(manuel=True, noms=[iid]))
        m.add_command(label="Tester l'accès au stockage (SSH + rsync)",
                      command=lambda: self.tester_acces(next(h for h in self.hotes if h.nom == iid), iid))
        m.add_separator()
        m.add_command(label="Préparer ce PC…", command=lambda: self.preparer(noms=[iid]))
        m.tk_popup(event.x_root, event.y_root)

    def tester_acces(self, h, titre):
        """Teste les liaisons PC → stockage utilisées par les ISO et les sauvegardes (à la demande seulement :
        des échecs répétés peuvent déclencher le blocage automatique d'un NAS)."""
        def travail():
            c = h.clone()
            try:
                return L.tester_acces_stockage(c, self.stockage)
            finally:
                c.fermer()

        def fin(res):
            lignes = []
            for etape, ok, det in res:
                L.log(titre, f"{'OK' if ok else 'ÉCHEC'} – {etape}" + ("" if ok else f" : {det}"),
                      "succes" if ok else "erreur")
                lignes.append(f"{'✔' if ok else '✘'}  {etape}" + ("" if ok else f"\n     {det}"))
            tout_ok = all(ok for _, ok, _ in res)
            (messagebox.showinfo if tout_ok else messagebox.showwarning)(
                f"Accès au stockage – {titre}", "\n\n".join(lignes))

        self.tache(f"Test de l'accès au stockage depuis {titre}", travail, fin)

    def details_serveur(self, nom):
        couleur, texte, det, heure = self.etats.get(nom, ("gris", "pas encore testé", "", ""))
        if nom == LOCAL:
            messagebox.showinfo("Ce poste", f"Compte : {getpass.getuser()}\nÉtat : {texte}"
                                + (f" (testé à {heure})" if heure else "") + (f"\n\n{det}" if det else "")
                                + "\n\nCe poste n'est jamais une cible de déploiement : il sert à piloter la "
                                  "ferme, exporter vos labos et recevoir des ISO.")
            return
        h = next(x for x in self.hotes if x.nom == nom)
        conseil = {"rouge": "\n\nVérifiez que le PC est allumé, branché au réseau et que le service SSH tourne "
                            "(sudo systemctl status ssh).",
                   "orange": "\n\nClic droit → « Préparer ce PC… » corrige généralement le problème "
                             "(le mot de passe root sera demandé si le compte n'est pas sudoer)."}.get(couleur, "")
        messagebox.showinfo(f"État de {nom}", f"{nom} – {h.ip}\nCompte : {h.utilisateur}\n"
                                              f"Pools : {', '.join(h.pools) or 'aucun'}\n"
                                              f"État : {texte}" + (f" (testé à {heure})" if heure else "") +
                            (f"\n\n{det}" if det else "") + conseil)

    def details_stockage(self):
        couleur, texte, det, heure = self.etat_stockage
        if messagebox.askyesno("Serveur de stockage", f"État : {texte or 'pas encore testé'}"
                               + (f" (testé à {heure})" if heure else "") + (f"\n\n{det}" if det else "")
                               + "\n\nTester maintenant l'accès SSH + rsync de ce poste vers le stockage ?"):
            self.tester_acces(L.hote_local(), "Ce poste")

    # ------------------------------------------------------------------ voyants (surveillance)

    def programmer_surveillance(self):
        if self._minuterie:
            self.after_cancel(self._minuterie)
            self._minuterie = None
        if self.var_auto.get():
            self._minuterie = self.after(INTERVALLES[self.var_intervalle.get()] * 1000, self._tic)

    def _tic(self):
        self._minuterie = None
        self.surveiller()
        self.programmer_surveillance()

    def surveiller(self, manuel=False, noms=None):
        """Teste les PC (et le stockage) en arrière-plan, sans bloquer les autres opérations."""
        if self._sonde_en_cours or not self.stockage:
            return
        self._sonde_en_cours = True
        hotes = [h for h in self.hotes if noms is None or h.nom in noms]
        if noms is None or LOCAL in noms:
            hotes = [L.hote_local()] + hotes
        stockage = self.stockage if noms is None else None
        premier = not self.etats
        for h in hotes:
            nom = LOCAL if h.local else h.nom
            if nom not in self.etats or manuel:
                ancien = self.etats.get(nom, ("gris", "", "", ""))
                self.etats[nom] = ("gris", "test…", ancien[2], ancien[3])
        self.afficher_serveurs()
        if manuel:
            L.log("voyants", f"test de {len(hotes)} PC…")

        def maj(nom, res, ancien):
            couleur, texte, det = res
            self.etats[nom] = (couleur, texte, det, time.strftime("%H:%M:%S"))
            if manuel or premier or ancien != couleur:
                L.log("ce poste" if nom == LOCAL else nom, f"{texte}" + (f" – {det}" if couleur != "vert" else ""),
                      {"vert": "succes", "orange": "alerte", "rouge": "erreur"}[couleur])
            else:
                L.detail(nom, f"voyant {couleur} : {texte}")
            self.afficher_serveurs()

        def maj_stockage(res):
            couleur, texte, det = res
            ancien = self.etat_stockage[0]
            self.etat_stockage = (couleur, texte, det, time.strftime("%H:%M:%S"))
            self.lbl_stockage.configure(image=self.pastilles[couleur])
            if manuel or premier or ancien != couleur:
                L.log("stockage", texte + (f" – {det}" if couleur != "vert" else ""),
                      {"vert": "succes", "orange": "alerte", "rouge": "erreur"}[couleur])

        anciens = {(LOCAL if h.local else h.nom): self.etats.get(LOCAL if h.local else h.nom, ("gris",))[0]
                   for h in hotes}
        if not premier:
            anciens = {n: (c if c != "gris" else None) for n, c in anciens.items()}

        def travail():
            try:
                with ThreadPoolExecutor(max_workers=16) as ex:
                    futurs = {ex.submit(L.sonder, h): h for h in hotes}
                    fs = ex.submit(L.sonder, stockage, True) if stockage else None
                    for f in as_completed(futurs):
                        h = futurs[f]
                        nom = LOCAL if h.local else h.nom
                        self.after(0, maj, nom, f.result(), anciens.get(nom))
                    if fs:
                        self.after(0, maj_stockage, fs.result())
            finally:
                self.after(0, fini)

        def fini():
            self._sonde_en_cours = False
            self.lbl_derniere.configure(text=f"dernier test : {time.strftime('%H:%M:%S')}")

        threading.Thread(target=travail, daemon=True).start()

    def _cocher(self, noms):
        self.coches = set(noms)
        self.afficher_serveurs()

    def cocher_pool(self):
        p = self.var_pool.get()
        self._cocher({h.nom for h in self.hotes if p in h.pools})

    def enregistrer_pool(self):
        if not self.coches:
            messagebox.showinfo("Pool", "Cochez d'abord les PC qui feront partie du pool.")
            return
        nom = simpledialog.askstring("Nouveau pool", "Nom du pool (les PC cochés en feront partie) :",
                                     initialvalue=self.var_pool.get(), parent=self)
        if not nom:
            return
        nom = nom.strip().replace(" ", "-")
        existe = any(nom in h.pools for h in self.hotes)
        if existe and not messagebox.askyesno("Pool", f"Le pool « {nom} » existe déjà. Le remplacer par les PC cochés ?"):
            return
        self._modifier_pools(lambda hn, pools: (pools | {nom}) if hn in self.coches else (pools - {nom}))
        self.var_pool.set(nom)
        L.log("config", f"pool « {nom} » : {', '.join(sorted(self.coches))}")

    def supprimer_pool(self):
        nom = self.var_pool.get()
        if not nom:
            messagebox.showinfo("Pool", "Choisissez un pool dans la liste.")
            return
        if messagebox.askyesno("Pool", f"Supprimer le pool « {nom} » ? (les PC restent dans la ferme)"):
            self._modifier_pools(lambda hn, pools: pools - {nom})
            self.var_pool.set("")
            L.log("config", f"pool « {nom} » supprimé")

    def _modifier_pools(self, regle):
        """Applique regle(nom_pc, set_pools) -> set_pools, puis enregistre ferme.yaml."""
        def appliquer(cfg):
            for h in cfg.get("hotes", []):
                h["pools"] = sorted(regle(h["nom"], set(h.get("pools") or [])))
        self._modifier_config(appliquer)

    def _modifier_config(self, modification):
        """Applique modification(cfg) au contenu de ferme.yaml puis l'enregistre et le recharge."""
        with open(self.chemin_config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        modification(cfg)
        mode = os.stat(self.chemin_config).st_mode & 0o777
        tmp = self.chemin_config + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(ENTETE_CONFIG)
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.chmod(tmp, mode)
        os.replace(tmp, self.chemin_config)
        self.charger_config()

    def tester_serveurs(self):
        self.surveiller(manuel=True)

    # ------------------------------------------------------------------ déployer / retirer

    def deployer(self):
        m = self.labo_choisi()
        cibles = self.pc_coches()
        if not m:
            messagebox.showinfo("Déployer", "Sélectionnez un labo dans la liste de gauche.")
            return
        if not cibles:
            messagebox.showinfo("Déployer", "Cochez au moins un PC (ou choisissez un pool).")
            return
        besoin = m.get("octets") or 0
        pleins = [h for h in cibles if L.ESPACES.get(h.nom) is not None
                  and L.ESPACES[h.nom] < besoin + L.MARGE_DISQUE]
        if pleins:
            liste = "\n".join(f"• {h.nom} : {L.taille_lisible(L.ESPACES[h.nom])} libres" for h in pleins)
            texte = (f"Le labo « {m['nom']} » demande {L.taille_lisible(besoin)} "
                     f"(+ {L.taille_lisible(L.MARGE_DISQUE)} de marge) sur chaque PC.\n\n"
                     f"Pas assez de place sur :\n{liste}")
            if len(pleins) == len(cibles):
                messagebox.showerror("Déployer", texte + "\n\nDéploiement annulé.")
                return
            if not messagebox.askyesno("Déployer", texte + "\n\nCes PC seront exclus. "
                                                           "Déployer sur les autres ?", icon="warning"):
                return
            cibles = [h for h in cibles if h not in pleins]
        hors = [h.nom for h in cibles if self.etats.get(h.nom, ("gris",))[0] in ("rouge", "orange")]
        if hors and not messagebox.askyesno(
                "Déployer", f"Ces PC ne sont pas prêts (voyant rouge ou orange) : {', '.join(hors)}.\n"
                            "Le déploiement y échouera probablement. Continuer quand même ?", icon="warning"):
            return
        sim = self.var_simulation.get()
        if not sim and not messagebox.askyesno(
                "Déployer", f"Déployer « {m['nom']} » ({len(m['vms'])} VM) sur {len(cibles)} PC :\n"
                            f"{', '.join(h.nom for h in cibles)} ?"):
            return
        args = argparse.Namespace(labo=m["nom"], tous=False, pool=None, hote=[h.nom for h in cibles],
                                  parallele=4, remplacer=self.var_remplacer.get(),
                                  remplacer_reseaux=self.var_remplacer_res.get(),
                                  conflit_ip=self.var_conflit.get(), demarrer=self.var_demarrer.get(),
                                  simulation=sim)

        def fin(res):
            ok, ko = res
            if ko:
                messagebox.showwarning("Déploiement", f"Réussi sur {len(ok)} PC.\nÉchec sur : {', '.join(ko)}\n"
                                                      "Voir le journal pour le détail.")
            else:
                messagebox.showinfo("Déploiement", f"{'Simulation terminée' if sim else 'Déploiement réussi'} "
                                                   f"sur {len(ok)} PC.")

        self.tache(f"Déploiement de {m['nom']}", lambda: L.cmd_deployer(args, self.stockage, self.hotes), fin)

    def retirer(self):
        m = self.labo_choisi()
        cibles = self.pc_coches()
        if not m or not cibles:
            messagebox.showinfo("Retirer", "Sélectionnez un labo et cochez les PC concernés.")
            return
        if not messagebox.askyesno("Retirer", f"Supprimer les VM, disques et réseaux du labo « {m['nom']} » sur :\n"
                                              f"{', '.join(h.nom for h in cibles)} ?\n\nCette action est définitive.",
                                   icon="warning"):
            return
        args = argparse.Namespace(labo=m["nom"], tous=False, pool=None, hote=[h.nom for h in cibles],
                                  parallele=4, garder_reseaux=False, oui=True)
        self.tache(f"Retrait de {m['nom']}", lambda: L.cmd_retirer(args, self.stockage, self.hotes),
                   lambda r: messagebox.showinfo("Retirer", f"Terminé : {len(r[0])} réussi(s), {len(r[1])} échec(s)."))

    # ------------------------------------------------------------------ préparation & export

    def _mdp_local(self, raison):
        return simpledialog.askstring(
            "Mot de passe", f"{raison}\n\nMot de passe de votre compte « {getpass.getuser()} » sur ce poste\n"
                            f"(si le compte n'est pas sudoer, le mot de passe root sera demandé ensuite) :",
            show="•", parent=self)

    def _demander_root(self, h):
        """Appelé depuis un thread de travail : demande le mot de passe root dans l'interface."""
        if self._root_commun:
            return self._root_commun
        evt, res = threading.Event(), {}

        def dialogue():
            mdp = simpledialog.askstring(
                "Mot de passe root",
                f"Le compte « {h.utilisateur} » de {'ce poste' if h.local else f'{h.nom} ({h.ip})'} "
                f"n'est pas sudoer.\n"
                f"Mot de passe root de ce PC (utilisé pour cette préparation, jamais enregistré) :",
                show="•", parent=self)
            if mdp and not h.local and messagebox.askyesno("Mot de passe root",
                                           "Utiliser ce même mot de passe root pour les autres PC "
                                           "de cette préparation qui en ont besoin ?", parent=self):
                self._root_commun = mdp
            res["mdp"] = mdp
            evt.set()

        self.after(0, dialogue)
        evt.wait()
        return res.get("mdp")

    def assurer_droits_locaux(self, suite):
        """Si ce poste est prêt, appelle suite() ; sinon propose de le préparer d'abord."""
        if L.local_pret():
            suite()
        elif messagebox.askyesno("Ce poste", "Ce poste n'est pas encore préparé (droits sur "
                                             "/var/lib/libvirt/images ou clé SSH manquants).\n\n"
                                             "Le préparer maintenant ? Seul ce poste est modifié."):
            self.preparer_local(suite)

    def preparer_local(self, suite=None):
        """Prépare uniquement ce poste (clé SSH vers le stockage + droits). Aucun autre PC n'est touché."""
        mdp = self._mdp_local("Préparation de ce poste : clé SSH vers le stockage et droits sur libvirt.")
        if mdp is None:
            return
        self._root_commun = None

        def fin(couleur):
            self.surveiller(manuel=True, noms=[LOCAL])
            if couleur == "vert" and suite:
                suite()
            elif couleur != "vert":
                messagebox.showwarning("Ce poste", "Préparation incomplète, voir le journal.")

        self.tache("Préparation de ce poste",
                   lambda: L.preparer_local(self.stockage, mdp, self._demander_root), fin)

    def preparer(self, noms=None):
        cibles = [h for h in self.hotes if h.nom in noms] if noms else (self.pc_coches() or self.hotes)
        if not messagebox.askyesno(
                "Préparer",
                "Cette opération utilise les mots de passe de la configuration pour :\n"
                "• installer les clés SSH (poste maître ↔ stockage ↔ serveurs),\n"
                "• autoriser virsh/rsync/qemu-img (règle sudo + groupe libvirt),\n"
                "• installer rsync, qemu-utils et sudo si nécessaire.\n\n"
                "Si un compte n'est pas sudoer, le mot de passe root de ce PC vous sera demandé.\n\n"
                f"PC concernés : {', '.join(h.nom for h in cibles)}\nContinuer ?"):
            return
        self._root_commun = None
        args = argparse.Namespace(tous=False, pool=None, hote=[h.nom for h in cibles],
                                  local=False, mdp_local=None, demander_root=self._demander_root)

        def fin(r):
            self._root_commun = None
            messagebox.showinfo("Préparation", "Préparation terminée (voir le journal).")
            self.surveiller(manuel=True)

        self.tache("Préparation", lambda: L.cmd_preparer(args, self.stockage, self.hotes), fin)

    def ouvrir_export(self):
        if not self.stockage:
            return
        self.assurer_droits_locaux(lambda: self.tache(
            "Lecture des labos de ce poste", lambda: L.labos_locaux(L.hote_local()),
            lambda reseaux: FenetreExport(self, reseaux)))

    def exporter(self, args):
        existe = args.nom in self.labos
        if existe and not messagebox.askyesno("Exporter", f"Le labo « {args.nom} » existe déjà sur le stockage.\n"
                                                          "Le remplacer par cette nouvelle version ?"):
            return False
        args.ecraser = existe
        self.tache(f"Export de {args.nom}", lambda: L.cmd_sauvegarder(args, self.stockage, self.hotes),
                   lambda r: (messagebox.showinfo("Export", f"Labo « {args.nom} » exporté sur le stockage."),
                              self.rafraichir_labos()))
        return True


class GraphiqueCharge(tk.Canvas):
    """Courbes CPU et RAM (%) des 10 dernières minutes, axe unique 0–100 %, survol = valeurs."""
    FENETRE = 600                       # secondes affichées
    G, D, H, B = 44, 104, 14, 22         # marges gauche / droite / haut / bas

    def __init__(self, parent, **kw):
        super().__init__(parent, height=190, background="#ffffff", highlightthickness=0, **kw)
        self.points = []
        self.bind("<Configure>", lambda e: self.dessiner())
        self.bind("<Motion>", self._survol)
        self.bind("<Leave>", lambda e: self.delete("survol"))

    def _xy(self, t, v, maintenant, w, h):
        x = self.G + (w - self.G - self.D) * (1 - (maintenant - t) / self.FENETRE)
        y = self.H + (h - self.H - self.B) * (1 - v / 100)
        return x, y

    def dessiner(self, points=None):
        if points is not None:
            self.points = points
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 200:
            return
        maintenant = time.time()
        # grille discrète et axe unique en %
        for v in (0, 25, 50, 75, 100):
            _, y = self._xy(maintenant, v, maintenant, w, h)
            self.create_line(self.G, y, w - self.D, y, fill="#ececec" if v else "#c8c8c8")
            self.create_text(self.G - 6, y, text=f"{v} %", anchor="e", fill="#6b6b6b", font=("TkDefaultFont", 8))
        # repères de seuil (ligne pointillée + libellé, pas de couleur seule)
        for v, txt in ((L.SEUIL_CRITIQUE, f"‼ {L.SEUIL_CRITIQUE} %"), (L.SEUIL_ATTENTION, f"⚠ {L.SEUIL_ATTENTION} %")):
            _, y = self._xy(maintenant, v, maintenant, w, h)
            self.create_line(self.G, y, w - self.D, y, fill="#d9a0a0" if v == L.SEUIL_CRITIQUE else "#e2c98a",
                             dash=(3, 3))
        for m in range(0, 11, 2):
            x, _ = self._xy(maintenant - m * 60, 0, maintenant, w, h)
            self.create_text(x, h - self.B + 10, text="maintenant" if m == 0 else f"-{m} min",
                             fill="#6b6b6b", font=("TkDefaultFont", 8))
        pts = [p for p in self.points if maintenant - p[0] <= self.FENETRE]
        if not pts:
            self.create_text(w / 2, h / 2, text="mesure en cours…", fill="#6b6b6b")
            return
        etiquettes = []
        for i, (couleur, nom, motif) in enumerate(((SERIE_CPU, "CPU", None), (SERIE_RAM, "RAM", (6, 3)))):
            coords = []
            for p in pts:
                coords += self._xy(p[0], p[1 + i], maintenant, w, h)
            if len(coords) >= 4:
                self.create_line(*coords, fill=couleur, width=2, dash=motif, capstyle="round", joinstyle="round")
            x, y = coords[-2], coords[-1]
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=couleur, outline="#ffffff", width=2)
            etiquettes.append([y, nom, pts[-1][1 + i], couleur, motif])
        # étiquettes directes en bout de courbe, décalées si elles se chevauchent
        etiquettes.sort()
        if len(etiquettes) == 2 and etiquettes[1][0] - etiquettes[0][0] < 14:
            milieu = (etiquettes[0][0] + etiquettes[1][0]) / 2
            etiquettes[0][0], etiquettes[1][0] = milieu - 8, milieu + 8
        for y, nom, v, couleur, motif in etiquettes:
            x0 = w - self.D + 8
            self.create_line(x0, y, x0 + 14, y, fill=couleur, width=2, dash=motif)
            self.create_text(x0 + 18, y, text=f"{nom} {v:.0f} %", anchor="w", fill="#1f1f1f",
                             font=("TkDefaultFont", 9))

    def _survol(self, e):
        self.delete("survol")
        w, h = self.winfo_width(), self.winfo_height()
        if not self.points or not (self.G <= e.x <= w - self.D):
            return
        maintenant = time.time()
        t = maintenant - self.FENETRE * (1 - (e.x - self.G) / (w - self.G - self.D))
        p = min(self.points, key=lambda q: abs(q[0] - t))
        if abs(p[0] - t) > 90:
            return
        x, _ = self._xy(p[0], 0, maintenant, w, h)
        self.create_line(x, self.H, x, h - self.B, fill="#9a9a9a", tags="survol")
        for i, couleur in ((1, SERIE_CPU), (2, SERIE_RAM)):
            _, y = self._xy(p[0], p[i], maintenant, w, h)
            self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=couleur, outline="#ffffff", width=2, tags="survol")
        texte = f"{time.strftime('%H:%M:%S', time.localtime(p[0]))}\nCPU {p[1]:.0f} %\nRAM {p[2]:.0f} %"
        bx = x + 10 if x + 120 < w else x - 110
        self.create_rectangle(bx, self.H + 4, bx + 100, self.H + 54, fill="#ffffff", outline="#cfcfcf",
                              tags="survol")
        self.create_text(bx + 8, self.H + 10, text=texte, anchor="nw", fill="#1f1f1f",
                         font=("TkDefaultFont", 8), tags="survol")


class FenetreMachine(tk.Toplevel):
    """Pilotage d'un PC comme dans virt-manager : réseaux, VM, instantanés, console."""

    def __init__(self, app, nom):
        super().__init__(app)
        self.app = app
        self.geometry("1050x800")
        self.donnees = None
        self._arret_mesure = threading.Event()
        self.protocol("WM_DELETE_WINDOW", self.fermer)

        haut = ttk.Frame(self, padding=(8, 8, 8, 4))
        haut.pack(fill="x")
        ttk.Label(haut, text="Machine :").pack(side="left")
        self.noms = {"Ce poste": LOCAL} | {h.nom: h.nom for h in app.hotes}
        self.var_machine = tk.StringVar(value=next(k for k, v in self.noms.items() if v == nom))
        cb = ttk.Combobox(haut, textvariable=self.var_machine, values=list(self.noms), state="readonly", width=22)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: (self.actualiser(), self.demarrer_mesure()))
        ttk.Button(haut, text="⟳ Actualiser", command=self.actualiser).pack(side="left", padx=4)
        self.lbl = ttk.Label(haut, foreground="#666")
        self.lbl.pack(side="left", padx=10)
        ttk.Button(haut, text="Ouvrir dans virt-manager", command=self.ouvrir_virt_manager).pack(side="right")

        charge = ttk.LabelFrame(self, text="Charge en direct (mesure toutes les 3 s)", padding=6)
        charge.pack(fill="x", padx=8, pady=(0, 6))
        ligne = ttk.Frame(charge)
        ligne.pack(fill="x")
        self.lbl_etat_charge = ttk.Label(ligne, compound="left", font=("TkDefaultFont", 10, "bold"))
        self.lbl_etat_charge.pack(side="left")
        self.lbl_charge = ttk.Label(ligne, foreground="#333")
        self.lbl_charge.pack(side="left", padx=12)
        legende = ttk.Frame(ligne)
        legende.pack(side="right")
        for couleur, nom, motif in ((SERIE_CPU, "CPU", None), (SERIE_RAM, "RAM", (6, 3))):
            c = tk.Canvas(legende, width=22, height=10, highlightthickness=0, background=self.cget("background"))
            c.create_line(2, 5, 20, 5, fill=couleur, width=2, dash=motif)
            c.pack(side="left")
            ttk.Label(legende, text=nom + "   ").pack(side="left")
        self.graphique = GraphiqueCharge(charge)
        self.graphique.pack(fill="x", pady=(4, 0))

        corps = ttk.Frame(self, padding=(8, 0, 8, 0))
        corps.pack(fill="both", expand=True)
        cols = ("etat", "infos", "auto", "snaps")
        self.arbre = ttk.Treeview(corps, columns=cols, show="tree headings", selectmode="browse")
        self.arbre.heading("#0", text="Nom")
        self.arbre.column("#0", width=280)
        for c, t, w in zip(cols, ("État", "Détails", "Démarrage auto", "Instantanés"), (110, 330, 110, 90)):
            self.arbre.heading(c, text=t)
            self.arbre.column(c, width=w, anchor="w" if c == "infos" else "center")
        aligner_entetes(self.arbre)
        sy = ttk.Scrollbar(corps, command=self.arbre.yview)
        self.arbre.configure(yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        self.arbre.pack(fill="both", expand=True)
        for c in COULEURS:
            self.arbre.tag_configure(c, foreground={"vert": "#1a7f37", "orange": "#8a5a00",
                                                    "rouge": "#a40e26", "gris": "#555"}[c])
        self.arbre.tag_configure("titre", font=("TkDefaultFont", 10, "bold"))
        self.arbre.bind("<<TreeviewSelect>>", lambda e: self.maj_boutons())
        self.arbre.bind("<Double-Button-1>", lambda e: self.console())

        self.barre = ttk.Frame(self, padding=8)
        self.barre.pack(fill="x")
        self.boutons = {}
        ligne1, ligne2 = ttk.Frame(self.barre), ttk.Frame(self.barre)
        ligne1.pack(fill="x")
        ligne2.pack(fill="x")
        for cle, texte, cmd in (
                ("demarrer", "▶ Démarrer", lambda: self.agir("demarrer")),
                ("arreter", "⏻ Arrêter", lambda: self.agir("arreter")),
                ("forcer", "⏹ Forcer l'arrêt", lambda: self.agir("forcer")),
                ("redemarrer", "⟲ Redémarrer", lambda: self.agir("redemarrer")),
                ("pause", "⏸ Pause", lambda: self.agir("pause")),
                ("reprendre", "⏵ Reprendre", lambda: self.agir("reprendre")),
                ("auto", "Démarrage auto", self.basculer_auto),
                ("console", "🖥 Console", self.console),
                ("snaps", "📷 Instantanés…", self.instantanes),
                ("supprimer", "🗑 Supprimer…", self.supprimer)):
            ligne = ligne1 if cle in ("demarrer", "arreter", "forcer", "redemarrer", "pause", "reprendre") else ligne2
            b = ttk.Button(ligne, text=texte, command=cmd)
            b.pack(side="left", padx=2, pady=2)
            self.boutons[cle] = b
        self.lbl_aide = ttk.Label(self, foreground="#666", padding=(8, 0, 8, 8),
                                  text="Double-clic sur une VM : ouvrir sa console. « Arrêter » demande un arrêt "
                                       "propre au système invité ; « Forcer l'arrêt » équivaut à débrancher la prise.")
        self.lbl_aide.pack(fill="x")
        self.actualiser()
        self.demarrer_mesure()

    # -- charge en direct
    def demarrer_mesure(self):
        """Mesure CPU/RAM toutes les 3 s sur une connexion dédiée (indépendante des autres opérations)."""
        self._arret_mesure.set()
        arret = self._arret_mesure = threading.Event()
        hote = self.hote().clone()
        nom = hote.nom
        self.graphique.dessiner(list(L.HISTORIQUE.get(nom, [])))
        self.lbl_charge.configure(text="mesure…")

        def boucle():
            try:
                while not arret.is_set():
                    try:
                        m = L.charge_hote(hote)
                        self.after(0, lambda m=m: self._afficher_charge(nom, m, arret))
                    except Exception as e:
                        self.after(0, lambda e=e: arret.is_set() or self.lbl_charge.configure(
                            text=f"mesure impossible : {str(e).splitlines()[0][:80]}"))
                        hote.fermer()
                    arret.wait(2)
            finally:
                hote.fermer()

        threading.Thread(target=boucle, daemon=True).start()

    def _afficher_charge(self, nom, m, arret):
        if arret.is_set() or not self.winfo_exists():
            return
        niveau = L.niveau_charge(m)
        libelle = {"vert": "OK", "orange": "⚠ Charge élevée", "rouge": "‼ Charge critique"}[niveau]
        self.lbl_etat_charge.configure(image=self.app.pastilles_charge[niveau], text=" " + libelle)
        self.lbl_charge.configure(
            text=f"CPU {m['cpu']:.0f} % sur {m['coeurs']} cœur(s) · charge moyenne {m['charge1']:.2f} · "
                 f"RAM {m['ram']:.0f} % ({L.taille_lisible(m['ram_utilisee'])} / {L.taille_lisible(m['ram_totale'])})")
        self.graphique.dessiner(list(L.HISTORIQUE.get(nom, [])))

    def fermer(self):
        self._arret_mesure.set()
        self.destroy()

    # -- données
    def hote(self):
        return self.app.hote_par_nom(self.noms[self.var_machine.get()])

    def selection(self):
        sel = self.arbre.selection()
        if not sel or ":" not in sel[0]:
            return None, None
        genre, _, nom = sel[0].partition(":")
        return genre, nom

    def _executer(self, titre, fonction, apres=None):
        h = self.hote().clone()

        def travail():
            try:
                return fonction(h)
            finally:
                h.fermer()
        self.app.tache(titre, travail, apres or (lambda r: self.actualiser()))

    def actualiser(self):
        self.title(f"Vue machine – {self.var_machine.get()}")
        self.lbl.configure(text="lecture…")
        ancienne = self.arbre.selection()

        def fin(d):
            self.donnees = d
            self.afficher()
            if ancienne and self.arbre.exists(ancienne[0]):
                self.arbre.selection_set(ancienne[0])
            self.lbl.configure(text=f"{len(d['vms'])} VM · {len(d['reseaux'])} réseau(x) · "
                                    f"{L.taille_lisible(d['libre']) if d['libre'] else '?'} libres · "
                                    f"{time.strftime('%H:%M:%S')}")
        self._executer("Lecture de la machine", L.details_machine, fin)

    def afficher(self):
        a = self.arbre
        a.delete(*a.get_children())
        d = self.donnees
        a.insert("", "end", iid="vms", text=f"Machines virtuelles ({len(d['vms'])})", open=True, tags=("titre",))
        for v in d["vms"]:
            coul = {"running": "vert", "paused": "orange", "shut off": "gris"}.get(v["etat"], "rouge")
            a.insert("vms", "end", iid="vm:" + v["nom"], text=f"  {v['nom']}", image=self.app.pastilles[coul],
                     tags=(coul,), values=(v["etat_fr"],
                                           f"{v['vcpu']} vCPU · {L.taille_lisible(v['ram'])} RAM · "
                                           f"réseau : {', '.join(v['reseaux']) or '-'}",
                                           "oui" if v["autostart"] else "non", v["snapshots"] or ""))
        a.insert("", "end", iid="nets", text=f"Réseaux virtuels ({len(d['reseaux'])})", open=True, tags=("titre",))
        for r in d["reseaux"]:
            coul = "vert" if r["actif"] else "gris"
            a.insert("nets", "end", iid="net:" + r["nom"], text=f"  {r['nom']}", image=self.app.pastilles[coul],
                     tags=(coul,), values=("actif" if r["actif"] else "inactif",
                                           f"{r['mode']} · pont {r['pont'] or '-'} · "
                                           f"{', '.join(r['subnets']) or 'sans IP'}",
                                           "oui" if r["autostart"] else "non", ""))
        self.maj_boutons()

    def element(self):
        genre, nom = self.selection()
        if genre == "vm":
            return genre, next(v for v in self.donnees["vms"] if v["nom"] == nom)
        if genre == "net":
            return genre, next(r for r in self.donnees["reseaux"] if r["nom"] == nom)
        return None, None

    def maj_boutons(self):
        genre, e = self.element()
        actifs = set()
        if genre == "vm":
            etat = e["etat"]
            actifs = {"auto", "snaps", "supprimer"}
            if etat == "shut off":
                actifs |= {"demarrer"}
            elif etat == "paused":
                actifs |= {"reprendre", "forcer", "console"}
            else:
                actifs |= {"arreter", "forcer", "redemarrer", "pause", "console"}
            self.boutons["auto"].configure(text="Démarrage auto : " + ("désactiver" if e["autostart"] else "activer"))
        elif genre == "net":
            actifs = {"auto", "arreter" if e["actif"] else "demarrer"}
            self.boutons["auto"].configure(text="Démarrage auto : " + ("désactiver" if e["autostart"] else "activer"))
        else:
            self.boutons["auto"].configure(text="Démarrage auto")
        for k, b in self.boutons.items():
            b.configure(state="normal" if k in actifs else "disabled")

    # -- actions
    def agir(self, action):
        genre, e = self.element()
        if not e:
            return
        if action == "forcer" and not messagebox.askyesno(
                "Forcer l'arrêt", f"Couper brutalement « {e['nom']} » (comme débrancher la prise) ?", parent=self):
            return
        if genre == "net" and action == "arreter":
            vms = [v["nom"] for v in self.donnees["vms"] if e["nom"] in v["reseaux"] and v["etat"] == "running"]
            if vms and not messagebox.askyesno("Arrêter le réseau", f"VM en marche sur ce réseau : "
                                               f"{', '.join(vms)}.\nElles perdront le réseau. Continuer ?",
                                               parent=self):
                return
        f = L.action_vm if genre == "vm" else L.action_reseau
        self._executer(f"{action} {e['nom']}", lambda h: f(h, e["nom"], action),
                       lambda r: self.after(1500 if action == "arreter" else 0, self.actualiser))

    def basculer_auto(self):
        genre, e = self.element()
        if e:
            action = "autostart-off" if e["autostart"] else "autostart-on"
            f = L.action_vm if genre == "vm" else L.action_reseau
            self._executer("Démarrage automatique", lambda h: f(h, e["nom"], action))

    def supprimer(self):
        genre, e = self.element()
        if genre != "vm":
            return
        if not messagebox.askyesno("Supprimer la VM",
                                   f"Supprimer définitivement « {e['nom']} » de {self.var_machine.get()}, "
                                   f"avec ses disques et ses instantanés ?\n\nCette action est irréversible.",
                                   icon="warning", parent=self):
            return
        self._executer(f"Suppression de {e['nom']}", lambda h: L.action_vm(h, e["nom"], "supprimer"))

    def instantanes(self):
        genre, e = self.element()
        if genre == "vm":
            FenetreInstantanes(self, e["nom"])

    def _lancer(self, commande):
        try:
            subprocess.Popen(commande, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            L.log("console", " ".join(commande[:3]) + " …")
            return True
        except FileNotFoundError:
            return False

    def console(self):
        genre, e = self.element()
        if genre != "vm" or e["etat"] == "shut off":
            return
        uri = L.uri_libvirt(self.hote())
        if not (self._lancer(["virt-viewer", "-c", uri, "--attach", e["nom"]]) or
                self._lancer(["virt-manager", "-c", uri, "--show-domain-console", e["nom"]])):
            messagebox.showerror("Console", "Installez virt-viewer (ou virt-manager) sur ce poste :\n"
                                            "sudo apt install virt-viewer", parent=self)

    def ouvrir_virt_manager(self):
        if not self._lancer(["virt-manager", "-c", L.uri_libvirt(self.hote())]):
            messagebox.showerror("virt-manager", "virt-manager n'est pas installé sur ce poste.", parent=self)


class FenetreInstantanes(tk.Toplevel):
    def __init__(self, parent, vm):
        super().__init__(parent)
        self.parent, self.vm = parent, vm
        self.title(f"Instantanés – {vm} ({parent.var_machine.get()})")
        self.geometry("640x380")
        self.transient(parent)
        f = ttk.Frame(self, padding=8)
        f.pack(fill="both", expand=True)
        self.arbre = ttk.Treeview(f, columns=("date", "etat"), show="tree headings", selectmode="browse")
        self.arbre.heading("#0", text="Instantané")
        self.arbre.column("#0", width=260)
        self.arbre.heading("date", text="Date")
        self.arbre.column("date", width=160)
        self.arbre.heading("etat", text="État de la VM")
        self.arbre.column("etat", width=120)
        self.arbre.pack(fill="both", expand=True)
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="📷 Créer…", command=self.creer).pack(side="left")
        ttk.Button(b, text="↩ Restaurer", command=self.restaurer).pack(side="left", padx=6)
        ttk.Button(b, text="🗑 Supprimer", command=self.supprimer).pack(side="left")
        ttk.Button(b, text="Fermer", command=self.destroy).pack(side="right")
        ttk.Label(f, foreground="#666", wraplength=600, text="★ = instantané courant. Restaurer remet la VM "
                  "exactement dans l'état de l'instantané (les modifications faites depuis sont perdues).").pack(
            fill="x", pady=(6, 0))
        self.actualiser()

    def actualiser(self):
        def fin(snaps):
            self.arbre.delete(*self.arbre.get_children())
            for s in snaps:
                self.arbre.insert("", "end", iid=s["nom"], text=("★ " if s["courant"] else "   ") + s["nom"],
                                  values=(s["date"], s["etat"]))
        self.parent._executer("Lecture des instantanés", lambda h: L.lister_snapshots(h, self.vm), fin)

    def choisi(self):
        sel = self.arbre.selection()
        if not sel:
            messagebox.showinfo("Instantanés", "Sélectionnez un instantané.", parent=self)
        return sel[0] if sel else None

    def _puis(self):
        return lambda r: (self.actualiser(), self.parent.actualiser())

    def creer(self):
        nom = simpledialog.askstring("Nouvel instantané", "Nom (lettres, chiffres, . _ -) :",
                                     initialvalue=time.strftime("instantane-%Y%m%d-%H%M"), parent=self)
        if not nom:
            return
        desc = simpledialog.askstring("Nouvel instantané", "Description (facultatif) :", parent=self) or ""
        self.parent._executer(f"Instantané {nom}", lambda h: L.creer_snapshot(h, self.vm, nom.strip(), desc),
                              self._puis())

    def restaurer(self):
        nom = self.choisi()
        if nom and messagebox.askyesno("Restaurer", f"Remettre « {self.vm} » dans l'état de « {nom} » ?\n"
                                                    "Les modifications faites depuis seront perdues.",
                                       icon="warning", parent=self):
            self.parent._executer(f"Restauration de {nom}", lambda h: L.restaurer_snapshot(h, self.vm, nom),
                                  self._puis())

    def supprimer(self):
        nom = self.choisi()
        if nom and messagebox.askyesno("Supprimer", f"Supprimer l'instantané « {nom} » ?", parent=self):
            self.parent._executer(f"Suppression de {nom}", lambda h: L.supprimer_snapshot(h, self.vm, nom),
                                  self._puis())


class FenetreFerme(tk.Toplevel):
    """Tableau croisé : labos du stockage × PC de la ferme."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Vue de la ferme – labos déployés")
        self.geometry("1150x640")
        self.inventaires, self.erreurs = {}, {}

        haut = ttk.Frame(self, padding=(8, 8, 8, 4))
        haut.pack(fill="x")
        ttk.Label(haut, text="Labos déployés sur la ferme", style="Titre.TLabel").pack(side="left")
        ttk.Button(haut, text="⟳ Actualiser", command=self.actualiser).pack(side="right")
        self.lbl = ttk.Label(haut, foreground="#666")
        self.lbl.pack(side="right", padx=10)
        ttk.Label(self, foreground="#555", padding=(8, 0),
                  text="✔ complet   ◐ partiel (VM ou réseau manquant)   ▶ n = VM en marche   "
                       "vide = absent   ✖ = PC injoignable").pack(anchor="w")

        panneaux = ttk.PanedWindow(self, orient="vertical")
        panneaux.pack(fill="both", expand=True, padx=8, pady=6)
        cadre = ttk.Frame(panneaux)
        self.arbre = ttk.Treeview(cadre, show="tree headings", selectmode="browse")
        sx = ttk.Scrollbar(cadre, orient="horizontal", command=self.arbre.xview)
        sy = ttk.Scrollbar(cadre, command=self.arbre.yview)
        self.arbre.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.arbre.pack(fill="both", expand=True)
        self.arbre.tag_configure("autres", foreground="#666", background="#f3f3f3")
        self.arbre.bind("<<TreeviewSelect>>", lambda e: self.details())
        panneaux.add(cadre, weight=3)

        bas = ttk.Frame(panneaux)
        self.txt = tk.Text(bas, height=9, wrap="word", relief="flat", background=T["surface"],
                           font=(app.police, 10), foreground=T["encre"],
                           highlightthickness=1, highlightbackground=T["ligne"], padx=8, pady=6)
        self.txt.pack(fill="both", expand=True)
        b = ttk.Frame(bas)
        b.pack(fill="x", pady=(4, 0))
        self.btn = ttk.Button(b, text="Sélectionner ce labo et cocher les PC où il est déployé",
                              command=self.vers_principale, state="disabled")
        self.btn.pack(side="left")
        panneaux.add(bas, weight=1)
        self.actualiser()

    def actualiser(self):
        app = self.app
        hotes = list(app.hotes)
        self.lbl.configure(text="lecture des PC…")

        def travail():
            inv, err = {}, {}

            def un(h):
                c = h.clone()
                try:
                    socket.create_connection((c.ip, 22), timeout=3).close()   # PC éteint : réponse rapide
                    return h.nom, L.inventaire_hote(c), None
                except Exception as e:
                    return h.nom, None, str(e).splitlines()[0][:150]
                finally:
                    c.fermer()

            with ThreadPoolExecutor(max_workers=16) as ex:
                for nom, i, e in ex.map(un, hotes):
                    if i is None:
                        err[nom] = e
                    else:
                        inv[nom] = i
            return inv, err

        def fin(res):
            self.inventaires, self.erreurs = res
            self.afficher()
            self.lbl.configure(text=f"mis à jour à {time.strftime('%H:%M:%S')} · "
                                    f"{len(self.inventaires)} PC lus, {len(self.erreurs)} injoignable(s)")

        app.tache("Lecture de la ferme", travail, fin)

    def afficher(self):
        a = self.arbre
        a.delete(*a.get_children())
        hotes = [h.nom for h in self.app.hotes]
        a.configure(columns=["nb"] + hotes)
        a.heading("#0", text="Labo")
        a.column("#0", width=200, stretch=False)
        a.heading("nb", text="Déployé sur")
        a.column("nb", width=90, anchor="center", stretch=False)
        for n in hotes:
            a.heading(n, text=n)
            a.column(n, width=95, anchor="center", stretch=False)
        self.etats = {}
        for m in sorted(self.app.labos.values(), key=lambda x: x["nom"].lower()):
            cases, nb = [], 0
            for n in hotes:
                if n in self.erreurs:
                    cases.append("✖")
                    continue
                e = L.etat_labo(m, self.inventaires[n])
                self.etats[(m["nom"], n)] = e
                if e is None:
                    cases.append("")
                else:
                    nb += 1
                    cases.append(("✔ " if e["complet"] else f"◐ {len(e['presentes'])}/{e['total']} ")
                                 + (f"▶{len(e['en_marche'])}" if e["en_marche"] else ""))
            a.insert("", "end", iid="labo:" + m["nom"], text=f"  {m['nom']}", values=[f"{nb} PC"] + cases)
        cases = []
        for n in hotes:
            if n in self.erreurs:
                cases.append("✖")
            else:
                vms, nets = L.hors_labos(self.app.labos.values(), self.inventaires[n])
                cases.append(f"{len(vms)} VM, {len(nets)} rés." if vms or nets else "")
        a.insert("", "end", iid="autres", text="  (hors labos du stockage)", tags=("autres",),
                 values=[""] + cases)
        self.details()

    def details(self):
        sel = self.arbre.selection()
        t = ""
        self.btn.configure(state="disabled")
        if not sel:
            t = "Sélectionnez une ligne pour voir le détail par PC."
        elif sel[0] == "autres":
            t = "VM et réseaux présents sur les PC mais absents des labos du stockage " \
                "(labos de collègues non exportés, VM créées à la main…) :\n"
            for h in self.app.hotes:
                if h.nom in self.inventaires:
                    vms, nets = L.hors_labos(self.app.labos.values(), self.inventaires[h.nom])
                    if vms or nets:
                        inv = self.inventaires[h.nom]
                        t += f"\n{h.nom} :\n"
                        if vms:
                            t += "   VM : " + ", ".join(f"{v}{' ▶' if inv['vms'][v] else ''}" for v in vms) + "\n"
                        if nets:
                            t += "   réseaux : " + ", ".join(nets) + "\n"
        else:
            labo = sel[0][5:]
            t = f"{labo}\n"
            for h in self.app.hotes:
                if h.nom in self.erreurs:
                    t += f"\n✖ {h.nom} : injoignable ({self.erreurs[h.nom]})"
                    continue
                e = self.etats.get((labo, h.nom))
                if e is None:
                    continue
                t += f"\n{'✔' if e['complet'] else '◐'} {h.nom} : {len(e['presentes'])}/{e['total']} VM"
                if e["en_marche"]:
                    t += f", en marche : {', '.join(e['en_marche'])}"
                if e["manquantes"]:
                    t += f"\n      VM manquantes : {', '.join(e['manquantes'])}"
                if e["reseaux_manquants"]:
                    t += f"\n      réseaux manquants : {', '.join(e['reseaux_manquants'])}"
            if t.count("\n") == 0:
                t += "\nDéployé sur aucun PC."
            else:
                self.btn.configure(state="normal")
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("1.0", t)
        self.txt.configure(state="disabled")

    def vers_principale(self):
        sel = self.arbre.selection()
        if sel and sel[0].startswith("labo:"):
            labo = sel[0][5:]
            noms = [h.nom for h in self.app.hotes if self.etats.get((labo, h.nom))]
            self.app.selectionner_labo_et_pc(labo, noms)


class FenetreJournal(tk.Toplevel):
    """Affichage du fichier journal, avec filtres et suivi en direct."""
    MOTIF = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+)\s+(.*)$")
    RANG = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 3}
    FILTRES = {"Tout (y compris détails techniques)": 0, "Messages": 1,
               "Avertissements et erreurs": 2, "Erreurs seulement": 3}
    MAX = 20000

    def __init__(self, app):
        super().__init__(app)
        self.title("Journal de l'application")
        self.geometry("1100x620")
        self.entrees = []                  # (date, niveau, texte)
        self.position = 0

        haut = ttk.Frame(self, padding=(8, 8, 8, 4))
        haut.pack(fill="x")
        ttk.Label(haut, text="Afficher :").pack(side="left")
        self.var_niveau = tk.StringVar(value="Messages")
        cb = ttk.Combobox(haut, textvariable=self.var_niveau, values=list(self.FILTRES), state="readonly", width=34)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.reafficher())
        ttk.Label(haut, text="Rechercher :").pack(side="left", padx=(12, 0))
        self.var_cherche = tk.StringVar()
        e = ttk.Entry(haut, textvariable=self.var_cherche, width=24)
        e.pack(side="left", padx=4)
        self.var_cherche.trace_add("write", lambda *a: self.reafficher())
        self.var_suivre = tk.BooleanVar(value=True)
        ttk.Checkbutton(haut, text="Suivre en direct", variable=self.var_suivre).pack(side="left", padx=12)
        ttk.Button(haut, text="Vider le journal…", command=self.vider).pack(side="right")
        ttk.Button(haut, text="Ouvrir le dossier", command=self.ouvrir_dossier).pack(side="right", padx=4)
        ttk.Button(haut, text="Copier", command=self.copier).pack(side="right")
        ttk.Button(haut, text="⟳", width=3, command=self.recharger).pack(side="right", padx=4)

        cadre = ttk.Frame(self, padding=(8, 0, 8, 0))
        cadre.pack(fill="both", expand=True)
        self.txt = tk.Text(cadre, wrap="none", font=("TkFixedFont", 9), background="#1e1e1e",
                           foreground="#dcdcdc", insertbackground="white")
        sy = ttk.Scrollbar(cadre, command=self.txt.yview)
        sx = ttk.Scrollbar(cadre, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.txt.pack(fill="both", expand=True)
        for tag, coul in (("DEBUG", "#7d8590"), ("WARNING", "#e3b341"), ("ERROR", "#ff7b72"),
                          ("CRITICAL", "#ff7b72"), ("date", "#7d8590")):
            self.txt.tag_configure(tag, foreground=coul)
        self.txt.tag_configure("trouve", background="#3a3a00")

        self.lbl = ttk.Label(self, foreground="#666", padding=(8, 2, 8, 6))
        self.lbl.pack(fill="x")
        self.recharger()
        self._sondage()

    def _analyser(self, texte):
        nouvelles = []
        for ligne in texte.splitlines():
            m = self.MOTIF.match(ligne)
            if m:
                nouvelles.append([m.group(1), m.group(2), m.group(3)])
            elif nouvelles:
                nouvelles[-1][2] += "\n" + ligne          # suite multi-ligne (trace, sortie de commande)
            elif self.entrees:
                self.entrees[-1][2] += "\n" + ligne
        return nouvelles

    def _lire(self, depuis):
        try:
            with open(L.FICHIER_LOG, encoding="utf-8", errors="replace") as f:
                f.seek(depuis)
                texte = f.read()
                return texte, f.tell()
        except FileNotFoundError:
            return "", 0

    def recharger(self):
        texte, self.position = self._lire(0)
        self.entrees = self._analyser(texte)[-self.MAX:]
        self.reafficher()

    def _visible(self, e):
        if self.RANG.get(e[1], 1) < self.FILTRES[self.var_niveau.get()]:
            return False
        c = self.var_cherche.get().strip().lower()
        return not c or c in e[2].lower()

    def _inserer(self, e):
        self.txt.insert("end", e[0] + " ", "date")
        self.txt.insert("end", e[2] + "\n", e[1])

    def reafficher(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        n = 0
        for e in self.entrees:
            if self._visible(e):
                self._inserer(e)
                n += 1
        self._surligner()
        self.txt.configure(state="disabled")
        self.txt.see("end")
        self._statut(n)

    def _surligner(self):
        c = self.var_cherche.get().strip()
        if not c:
            return
        debut = "1.0"
        while True:
            debut = self.txt.search(c, debut, "end", nocase=True)
            if not debut:
                break
            fin = f"{debut}+{len(c)}c"
            self.txt.tag_add("trouve", debut, fin)
            debut = fin

    def _statut(self, n):
        try:
            taille = os.path.getsize(L.FICHIER_LOG) / 1024
        except OSError:
            taille = 0
        self.lbl.configure(text=f"{L.FICHIER_LOG}  ·  {taille:.0f} Ko  ·  {n} ligne(s) affichée(s) "
                                f"sur {len(self.entrees)}")

    def _sondage(self):
        if not self.winfo_exists():
            return
        try:
            taille = os.path.getsize(L.FICHIER_LOG)
        except OSError:
            taille = 0
        if taille < self.position:                       # fichier vidé ou rotation
            self.recharger()
        elif taille > self.position:
            texte, self.position = self._lire(self.position)
            nouvelles = self._analyser(texte)
            self.entrees.extend(nouvelles)
            del self.entrees[:-self.MAX]
            self.txt.configure(state="normal")
            for e in nouvelles:
                if self._visible(e):
                    self._inserer(e)
            self._surligner()
            self.txt.configure(state="disabled")
            if self.var_suivre.get():
                self.txt.see("end")
            self._statut(int(self.txt.index("end-1c").split(".")[0]) - 1)
        self.after(1000, self._sondage)

    def copier(self):
        self.clipboard_clear()
        self.clipboard_append(self.txt.get("1.0", "end-1c"))
        self.lbl.configure(text="Contenu affiché copié dans le presse-papiers.")

    def ouvrir_dossier(self):
        try:
            subprocess.Popen(["xdg-open", os.path.dirname(L.FICHIER_LOG)])
        except OSError as e:
            messagebox.showerror("Journal", str(e), parent=self)

    def vider(self):
        if messagebox.askyesno("Journal", "Effacer tout le contenu du fichier journal ?", parent=self):
            open(L.FICHIER_LOG, "w").close()
            L.log("journal", "journal vidé")
            self.recharger()


class FenetreExport(tk.Toplevel):
    """Choix d'un labo local (réseaux virtuels + VM) à exporter vers le stockage."""

    def __init__(self, app, reseaux):
        super().__init__(app)
        self.app, self.reseaux = app, reseaux
        self.title("Exporter un labo de ce poste")
        self.geometry("720x560")
        self.transient(app)
        self.choisis = set()

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Cochez le ou les réseaux virtuels qui forment le labo. "
                          "Toutes les VM qui y sont branchées seront exportées.",
                  wraplength=680).pack(anchor="w")

        self.arbre = ttk.Treeview(f, columns=("pont", "ip", "etat"), show="tree headings", selectmode="none", height=12)
        self.arbre.heading("#0", text="  Réseau / VM")
        self.arbre.column("#0", width=280)
        for c, t, w in (("pont", "Pont", 80), ("ip", "Sous-réseau", 140), ("etat", "État", 130)):
            self.arbre.heading(c, text=t)
            self.arbre.column(c, width=w)
        self.arbre.pack(fill="both", expand=True, pady=6)
        self.arbre.bind("<Button-1>", self._clic)
        self.arbre.tag_configure("vm", foreground="#555")

        g = ttk.Frame(f)
        g.pack(fill="x")
        self.var_nom = tk.StringVar()
        self.var_prop = tk.StringVar(value=getpass.getuser())
        self.var_desc = tk.StringVar()
        for i, (lab, var) in enumerate((("Nom du labo :", self.var_nom), ("Propriétaire :", self.var_prop),
                                        ("Description :", self.var_desc))):
            ttk.Label(g, text=lab).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(g, textvariable=var, width=60).grid(row=i, column=1, sticky="we", padx=4)
        g.columnconfigure(1, weight=1)
        self.var_arreter = tk.BooleanVar(value=True)
        self.var_eteint = tk.BooleanVar()
        ttk.Checkbutton(g, text="Éteindre proprement les VM allumées pendant l'export (puis les relancer)",
                        variable=self.var_arreter).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(g, text="Laisser les VM éteintes après l'export",
                        variable=self.var_eteint).grid(row=4, column=0, columnspan=2, sticky="w")

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="Annuler", command=self.destroy).pack(side="right")
        ttk.Button(b, text="⬆  Exporter vers le stockage", style="Action.TButton",
                   command=self.valider).pack(side="right", padx=6)
        self.afficher()

    def afficher(self):
        self.arbre.delete(*self.arbre.get_children())
        for r in self.reseaux:
            nb = len(r["vms"])
            iid = "net:" + r["nom"]
            self.arbre.insert("", "end", iid=iid, open=True,
                              text=f" {COCHE if r['nom'] in self.choisis else VIDE}  {r['nom']}  ({nb} VM)",
                              values=(r["bridge"] or "-", ", ".join(r["subnets"]) or "-",
                                      f"{r['mode']}, {'actif' if r['actif'] else 'inactif'}"))
            for vm, allumee in r["vms"]:
                self.arbre.insert(iid, "end", text=f"      {vm}", tags=("vm",),
                                  values=("", "", "allumée" if allumee else "éteinte"))

    def _clic(self, event):
        iid = self.arbre.identify_row(event.y)
        if iid.startswith("net:"):
            nom = iid[4:]
            self.choisis ^= {nom}
            if not self.var_nom.get() or self.var_nom.get() in {r["nom"] for r in self.reseaux}:
                self.var_nom.set(sorted(self.choisis)[0] if self.choisis else "")
            self.afficher()

    def valider(self):
        if not self.choisis:
            messagebox.showinfo("Exporter", "Cochez au moins un réseau virtuel.", parent=self)
            return
        vms = {v for r in self.reseaux if r["nom"] in self.choisis for v, _ in r["vms"]}
        if not vms:
            messagebox.showinfo("Exporter", "Aucune VM n'est branchée sur ce(s) réseau(x).", parent=self)
            return
        nom = self.var_nom.get().strip()
        if not L.NOM_VALIDE.match(nom):
            messagebox.showerror("Exporter", "Nom de labo invalide : lettres, chiffres, point, tiret, souligné.",
                                 parent=self)
            return
        allumees = [v for r in self.reseaux if r["nom"] in self.choisis for v, a in r["vms"] if a]
        if allumees and not self.var_arreter.get():
            messagebox.showerror("Exporter", f"VM allumées : {', '.join(sorted(set(allumees)))}.\n"
                                             "Éteignez-les ou cochez l'option d'arrêt.", parent=self)
            return
        args = argparse.Namespace(reseau=sorted(self.choisis), vm=None, nom=nom, hote=None,
                                  proprietaire=self.var_prop.get().strip() or None,
                                  description=self.var_desc.get().strip(), arreter=self.var_arreter.get(),
                                  laisser_eteint=self.var_eteint.get(), ecraser=False)
        if self.app.exporter(args):
            self.destroy()


def main():
    p = argparse.ArgumentParser(description="Interface graphique de la ferme de labos KVM")
    p.add_argument("--version", action="version", version=f"Ferme KVM {L.__version__} ({L.DATE_VERSION})")
    p.add_argument("-c", "--config", default=os.environ.get(
        "LABFERME_CONFIG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ferme.yaml")))
    a = p.parse_args()
    Application(a.config).mainloop()


if __name__ == "__main__":
    main()
