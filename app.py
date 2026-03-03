import os
import time
import requests
import uuid
import json
import io
from PIL import Image
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
import google.generativeai as genai

# ==========================================
# 1. INITIALISATION & CORS
# ==========================================
app = FastAPI(title="Persée Beauty AI - Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 2. CONFIGURATION & CLÉS
# ==========================================
YOUCAM_API_KEY = os.environ.get("YOUCAM_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 🏆 L'URL CRUCIALE IDENTIFIÉE DANS TON VITE.CONFIG
YOUCAM_BASE = "https://yce-api-01.makeupar.com/s2s/v2.0"

# Initialisation des clients externes
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# En-têtes standards pour MakeupAR
def get_youcam_headers():
    return {
        "Authorization": f"Bearer {YOUCAM_API_KEY}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

# ==========================================
# 3. ROUTES API
# ==========================================

@app.get("/")
def read_root():
    return {"status": "online", "message": "Persée Backend on Render is Live 🚀"}

# --- CATALOGUE CHEVEUX ---
@app.get("/api/vto/templates")
def get_hair_templates():
    if not YOUCAM_API_KEY:
        raise HTTPException(status_code=500, detail="Clé YouCam manquante.")
    
    url = f"{YOUCAM_BASE}/task/template/hair-style"
    res = requests.get(url, headers=get_youcam_headers())
    
    if not res.ok:
        raise HTTPException(status_code=res.status_code, detail=f"Erreur Catalogue: {res.text}")
    return res.json()

# --- GÉNÉRATION VTO (CHEVEUX) ---
@app.post("/api/vto/generate")
async def generate_vto(
    file: UploadFile = File(...),
    feature_type: str = Form(...),
    template_id: str = Form(...)
):
    if not YOUCAM_API_KEY:
        raise HTTPException(status_code=500, detail="Clé YouCam manquante.")

    try:
        file_bytes = await file.read()
        
        # ÉTAPE A : Enregistrement (Parsing flexible comme ton ancien code local)
        reg_payload = {
            "files": [{
                "content_type": "image/png",
                "file_name": "upload.jpg",
                "file_size": len(file_bytes)
            }]
        }
        reg_res = requests.post(f"{YOUCAM_BASE}/file/{feature_type}", headers=get_youcam_headers(), json=reg_payload)
        reg_data = reg_res.json()

        # On cherche les infos dans 'data' ou à la racine
        root = reg_data.get("data", reg_data)
        file_list = root.get("files", [])
        if not file_list:
            raise HTTPException(status_code=500, detail=f"Réponse MakeupAR incomplète: {reg_data}")
            
        file_entry = file_list[0]
        file_id = file_entry.get("file_id")
        
        # Extraction de l'URL d'upload (supporte plusieurs formats de réponse)
        upload_info = file_entry.get("requests", [{}])[0] if "requests" in file_entry else file_entry.get("request", {})
        upload_url = upload_info.get("url")
        upload_headers = upload_info.get("headers", {})

        # ÉTAPE B : Upload binaire S3
        put_headers = {"Content-Type": "image/png"}
        put_headers.update(upload_headers)
        requests.put(upload_url, headers=put_headers, data=file_bytes).raise_for_status()

        # ÉTAPE C : Lancement de la tâche IA
        task_res = requests.post(
            f"{YOUCAM_BASE}/task/{feature_type}",
            headers=get_youcam_headers(),
            json={"src_file_id": file_id, "template_id": template_id}
        )
        
        if not task_res.ok:
            raise HTTPException(status_code=task_res.status_code, detail=task_res.text)
            
        task_id = task_res.json().get("data", {}).get("task_id")

        # ÉTAPE D : Polling (Attente du résultat)
        for _ in range(30):
            time.sleep(2)
            poll_res = requests.get(f"{YOUCAM_BASE}/task/{feature_type}/{task_id}", headers=get_youcam_headers())
            poll_data = poll_res.json().get("data", {})
            status = str(poll_data.get("task_status", "")).lower()
            
            if status in ["done", "completed", "success"]:
                results = poll_data.get("results")
                # Gestion flexible : liste ou dictionnaire
                final_url = results[0].get("url") if isinstance(results, list) else results.get("url")
                
                # Sauvegarde Supabase
                if supabase:
                    try:
                        supabase.table("vto_generations").insert({
                            "session_id": str(uuid.uuid4()),
                            "feature_type": feature_type,
                            "template_id": template_id,
                            "result_image_url": final_url
                        }).execute()
                    except: pass
                
                return {"status": "success", "result_url": final_url}
            elif status in ["failed", "error"]:
                raise HTTPException(status_code=500, detail="L'IA MakeupAR a échoué.")

        raise HTTPException(status_code=408, detail="Timeout")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- DIAGNOSTIC DE PEAU (GEMINI 2.5 FLASH) ---
@app.post("/api/skin/diagnose")
async def diagnose_skin(file: UploadFile = File(...)):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Clé Gemini manquante.")

    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))

        # Prompt calibré pour ResultsDashboard.tsx
        prompt = """
        Analyse cette peau. Renvoie UNIQUEMENT un JSON valide avec cette structure :
        {
          "scores": {
            "acne": 20, "wrinkles": 15, "texture": 30, "spots": 10,
            "pores": 25, "eye_bags": 40, "redness": 5, "oiliness": 30,
            "moisture": 20, "firmness": 35, "radiance": 15,
            "droopy_upper_eyelid": 10, "droopy_lower_eyelid": 10
          },
          "skin_type": "Mixte",
          "skin_tone": "Fitzpatrick III",
          "skin_age": 28,
          "recommendation": "Conseil expert...",
          "product_keywords": ["Rétinol", "Vitamine C"],
          "detected_issues": ["Pores visibles"]
        }
        Scores de 0 (parfait) à 100 (sévère). Pas de markdown.
        """
        
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, image])
        text = response.text.replace('```json', '').replace('```', '').strip()
        
        data = json.loads(text)

        if supabase:
            try:
                supabase.table("skin_diagnostics").insert({
                    "session_id": str(uuid.uuid4()),
                    "overall_score": 100 - data["scores"]["texture"],
                    "recommendations": data["product_keywords"]
                }).execute()
            except: pass

        return {"status": "success", "data": data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))