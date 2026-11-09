# CommitMsg

Application de bureau (Windows) façon GitHub Desktop pour préparer un commit : choix des fichiers, aperçu du diff coloré,
message généré par Claude, commit et push.

## Fonctions

- Plusieurs dépôts mémorisés, navigation de branches (checkout, création), Fetch / Pull / Push.
- Onglet **Changements** : cases à cocher (clic, glisser, Maj+clic, Espace), aperçu du diff avec coloration syntaxique.
- Onglet **Historique** : liste des commits et diff par fichier.
- Message généré par l'IA au format `type(scope): Résumé` (`feat`, `fix`, `hotfix`, `feat(version X.Y.Z)`).
- Commit des seuls fichiers cochés, avec ou sans push.

## Prérequis

- Python 3.12+ (tkinter inclus) et Git.
- [Claude Code](https://claude.com/claude-code) installé et connecté (commande `claude`) pour la génération du message.

## Lancer

```bash
python app.py [chemin_du_repo]
```

## Construire l'exécutable

```bash
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name CommitMsg --noconfirm app.py
```

L'exécutable est créé dans `dist/CommitMsg.exe`. La liste des dépôts est enregistrée dans `%APPDATA%\CommitMsg\repos.json`.
