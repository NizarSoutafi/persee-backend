# 1. Image de base officielle Python (légère et sécurisée)
FROM python:3.10-slim

# 2. Définition du dossier de travail dans le conteneur
WORKDIR /app

# 3. Copie du fichier des dépendances en premier (pour optimiser le cache Docker)
COPY requirements.txt .

# 4. Installation des librairies sans garder de cache inutile
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copie de tout le reste du code (ton app.py)
COPY . .

# 6. Variables d'environnement par défaut (Render injectera son propre PORT dynamiquement)
ENV PORT=8000
EXPOSE $PORT

# 7. La commande pour allumer le serveur FastAPI
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}