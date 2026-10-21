# Historique des versions

| Version | Date | Contenu |
| --- | --- | --- |
| 1.1.0 | 2026-10-21 | Gestion de la ferme depuis l'interface : ajout, modification et retrait d'un PC (avec révocation de sa clé sur le stockage et nettoyage facultatif de la machine) ; configuration de l'emplacement de stockage avec test avant enregistrement, et création de `ferme.yaml` s'il n'existe pas ; export d'un labo depuis n'importe quelle machine : ce poste, un PC de la ferme ou une machine « source » (nouvelle section `sources`, jamais cible de déploiement), avec préparation de la machine depuis la fenêtre d'export. Nouvelles commandes `stockage`, `pc-ajouter`, `pc-retirer`. |
| 1.0.0 | 2026-10-03 | Première version de référence : export et déploiement de labos avec adaptation des ponts et sous-réseaux, pools, ISO, Vue de la ferme, Vue machine avec instantanés et charge en direct, voyants, contrôle de place, journal, interface à navigation latérale ; prise en charge des comptes non sudoers (via `su`) et des NAS Synology ; correction des VM issues d'un instantané qui ne démarraient pas après déploiement. |
