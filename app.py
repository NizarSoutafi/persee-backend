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
# 1. INITIALISATION
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
# 2. VARIABLES D'ENVIRONNEMENT
# ==========================================
YOUCAM_API_KEY = os.environ.get("YOUCAM_API_KEY")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

YOUCAM_BASE = "https://s2s.perfectcorp.com/s2s/v2.0"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ==========================================
# 3. ROUTES
# ==========================================

@app.get("/")
def read_root():
    return {"status": "online", "message": "Persée Backend is running 🚀"}

# --- CATALOGUE CHEVEUX ---
@app.get("/api/vto/templates")
def get_hair_templates():
    print("=== DÉBUT REQUÊTE CATALOGUE YOUCAM ===")
    print(f"YOUCAM_API_KEY présente: {bool(YOUCAM_API_KEY)}")
    print(f"YOUCAM_API_KEY début: {str(YOUCAM_API_KEY)[:15]}... if YOUCAM_API_KEY else 'NONE'")
    
    if not YOUCAM_API_KEY:
        print("ERREUR : Clé manquante dans les variables d'environnement HF.")
        raise HTTPException(status_code=500, detail="Clé YouCam manquante.")
    
    headers = {"Authorization": f"Bearer {YOUCAM_API_KEY}"}
    url = f"{YOUCAM_BASE}/task/template/hair-style"
    print(f"Appel URL: {url}")
    
    try:
        res = requests.get(url, headers=headers)
        print(f"Réponse YouCam Code: {res.status_code}")
        print(f"Réponse YouCam Texte: {res.text[:300]}")
        
        if not res.ok:
            raise HTTPException(status_code=res.status_code, detail=f"Erreur YouCam: {res.text}")
        
        print("=== SUCCÈS REQUÊTE CATALOGUE ===")
        return res.json()
    except Exception as e:
        print(f"EXCEPTION INTERNE CRITIQUE : {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# --- GÉNÉRATION VTO (CHEVEUX) ---
@app.post("/api/vto/generate")
async def generate_vto(
    file: UploadFile = File(...),
    feature_type: str = Form(...),
    template_id: str = Form(...)
):
    if not YOUCAM_API_KEY:
        raise HTTPException(status_code=500, detail="Clé YouCam manquante.")

    headers = {
        "Authorization": f"Bearer {YOUCAM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        file_bytes = await file.read()
        
        # ÉTAPE A : Enregistrement
        reg_payload = {
            "files": [{
                "content_type": file.content_type or "image/jpeg",
                "file_name": file.filename or "capture.jpg",
                "file_size": len(file_bytes)
            }]
        }
        reg_res = requests.post(f"{YOUCAM_BASE}/file/{feature_type}", headers=headers, json=reg_payload)
        reg_res.raise_for_status()
             
        reg_data = reg_res.json()
        file_info = reg_data.get("data", {}).get("files", [{}])[0]
        file_id = file_info.get("file_id")
        upload_request = file_info.get("requests", [{}])[0]
        upload_url = upload_request.get("url")
        upload_headers = upload_request.get("headers", {})

        if not file_id or not upload_url:
            raise HTTPException(status_code=500, detail="URL S3 introuvable.")

        # ÉTAPE B : Upload binaire
        put_headers = {"Content-Type": file.content_type or "image/jpeg"}
        put_headers.update(upload_headers)
        put_res = requests.put(upload_url, headers=put_headers, data=file_bytes)
        put_res.raise_for_status()

        # ÉTAPE C : Lancement tâche IA
        task_res = requests.post(
            f"{YOUCAM_BASE}/task/{feature_type}",
            headers=headers,
            json={"src_file_id": file_id, "template_id": template_id}
        )
        task_res.raise_for_status()
        task_id = task_res.json().get("data", {}).get("task_id")

        if not task_id:
            raise HTTPException(status_code=500, detail="Task ID introuvable.")

        # ÉTAPE D : Polling
        result_url = None
        for _ in range(60):
            time.sleep(2)
            poll_res = requests.get(
                f"{YOUCAM_BASE}/task/{feature_type}/{task_id}",
                headers={"Authorization": f"Bearer {YOUCAM_API_KEY}"}
            )
            if not poll_res.ok: continue
            
            poll_data = poll_res.json().get("data", {})
            status = str(poll_data.get("task_status", "")).lower()
            
            if status in ["done", "completed", "success", "finish", "finished"]:
                results = poll_data.get("results")
                if isinstance(results, list):
                    result_url = results[0].get("url") if results else None
                elif isinstance(results, dict):
                    result_url = results.get("url")
                break
            elif status in ["failed", "error", "fail"]:
                raise HTTPException(status_code=500, detail=f"Échec de l'IA YouCam: {poll_data}")

        if not result_url:
            raise HTTPException(status_code=408, detail="Timeout YouCam.")

        if supabase:
            try:
                supabase.table("vto_generations").insert({
                    "session_id": str(uuid.uuid4()),
                    "feature_type": feature_type,
                    "template_id": template_id,
                    "result_image_url": result_url
                }).execute()
            except Exception as e:
                print(f"Supabase warning: {e}")

        return {"status": "success", "result_url": result_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- DIAGNOSTIC DE PEAU (GEMINI) ---
@app.post("/api/skin/diagnose")
async def diagnose_skin(file: UploadFile = File(...)):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Clé Gemini manquante.")

    try:
        image_bytes = await file.read()
        image = Image.open(io.BytesIO(image_bytes))

        # PROMPT EXACT POUR TON FRONTEND
        prompt = """
        Tu es un dermatologue expert utilisant une IA avancée. Analyse cette image de peau avec une grande précision.
        Renvoie UNIQUEMENT un objet JSON valide, sans balises Markdown, respectant EXACTEMENT cette structure :
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
          "recommendation": "Texte détaillé de ton diagnostic et conseil...",
          "product_keywords": ["Acide Hyaluronique", "Niacinamide"],
          "detected_issues": ["Rougeurs locales", "Pores dilatés"]
        }
        Les scores doivent être entre 0 (excellent) et 100 (sévère).
        """
        
        # 🚀 MODÈLE GEMINI 2.5 FLASH ACTIVÉ COMME DEMANDÉ
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([prompt, image])
        response_text = response.text

        # Nettoyage de sécurité
        if response_text.strip().startswith("```json"):
            response_text = response_text.strip()[7:-3]
        elif response_text.strip().startswith("```"):
            response_text = response_text.strip()[3:-3]

        diagnostic_data = json.loads(response_text.strip())

        # Sauvegarde Supabase
        if supabase:
            try:
                scores = diagnostic_data.get("scores", {})
                supabase.table("skin_diagnostics").insert({
                    "session_id": str(uuid.uuid4()),
                    "overall_score": 100 - scores.get("texture", 0),
                    "acne_severity": str(scores.get("acne", 0)),
                    "wrinkles_severity": str(scores.get("wrinkles", 0)),
                    "hydration_level": str(scores.get("moisture", 0)),
                    "recommendations": diagnostic_data.get("product_keywords", [])
                }).execute()
            except Exception as e:
                print(f"Supabase warning: {e}")

        return {"status": "success", "data": diagnostic_data}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="L'IA n'a pas renvoyé de JSON valide.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
