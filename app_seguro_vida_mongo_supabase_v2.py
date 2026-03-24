import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError

# =========================
# Configuración base
# =========================
load_dotenv()
st.set_page_config(page_title="Asegura+ Vida", page_icon="🛡️", layout="wide")

def get_secret(name: str, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "asegura_vida_app.db")
CSV_EXPORT_FILE = os.path.join(BASE_DIR, "leads_seguro_vida_export.csv")
WHATSAPP_NUMBER = "525513207739"
APP_ENV = get_secret("APP_ENV", "prod")
DEV_PASSWORD = get_secret("ASEGURA_DEV_PASSWORD", "vida123")
DATA_BACKEND = str(get_secret("DATA_BACKEND", "supabase")).lower()  # supabase | mongo | sqlite
SUPABASE_URL = str(get_secret("SUPABASE_URL", "")).rstrip("/")
SUPABASE_KEY = str(get_secret("SUPABASE_KEY", ""))
SUPABASE_LEADS_TABLE = str(get_secret("SUPABASE_LEADS_TABLE", "leads"))
SUPABASE_LOGS_TABLE = str(get_secret("SUPABASE_LOGS_TABLE", "usage_logs"))
MONGODB_URI = str(get_secret("MONGODB_URI", ""))
MONGODB_DB = str(get_secret("MONGODB_DB", "asegura_vida"))
MONGODB_LEADS_COLLECTION = str(get_secret("MONGODB_LEADS_COLLECTION", "leads"))
MONGODB_LOGS_COLLECTION = str(get_secret("MONGODB_LOGS_COLLECTION", "usage_logs"))
ENABLE_LOGGING = APP_ENV in {"prod", "ngrok", "demo", "local"}

INSURERS = [
    {
        "name": "MetLife",
        "tag": "Respaldo global",
        "description": "Buena opción para protección familiar con procesos claros y marca reconocida.",
        "factor": 1.08,
        "accent": "#2563eb",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/c/c6/MetLife_logo.svg",
    },
    {
        "name": "GNP Seguros",
        "tag": "Trayectoria en México",
        "description": "Alternativa sólida para clientes que buscan cercanía, experiencia local y confianza.",
        "factor": 1.03,
        "accent": "#ea580c",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/2/26/GNP_Seguros_logo.svg",
    },
    {
        "name": "Insignia Life",
        "tag": "Propuesta flexible",
        "description": "Perfil adecuado para quienes valoran flexibilidad y atención más personalizada.",
        "factor": 1.00,
        "accent": "#7c3aed",
        "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/HD_transparent_picture.png/1px-HD_transparent_picture.png",
    },
]

FAQS = [
    (
        "¿Qué cubre un seguro de vida?",
        "Normalmente cubre el fallecimiento del asegurado. Según la póliza, también puede incluir invalidez total y permanente, muerte accidental, enfermedades graves o apoyo funerario.",
    ),
    (
        "¿Qué no cubre un seguro de vida?",
        "Depende del contrato, pero suelen existir exclusiones, periodos de espera, omisiones en la declaración y riesgos específicos no cubiertos por la aseguradora.",
    ),
]

USER_STORIES = [
    "Como cliente, quiero estimar una cobertura sugerida para entender qué nivel de protección podría necesitar mi familia.",
    "Como cliente, quiero recibir una prima mensual estimada para saber si el plan se ajusta a mi presupuesto.",
    "Como asesor, quiero guardar solicituds y su resultado para dar seguimiento comercial sin perder información.",
    "Como asesor, quiero revisar métricas y logs del uso de la app para validar que el prototipo funcione correctamente.",
]

KEY_METRICS_INFO = [
    "Cotizaciones generadas",
    "Prospectos guardados",
    "Errores de validación",
    "Tiempo promedio de respuesta",
    "Conversión a WhatsApp",
]

# =========================
# Persistencia y logging
# =========================
LEADS_COLUMNS = [
    "id", "created_at", "updated_at", "nombre", "correo", "telefono", "edad",
    "ingreso_mensual", "presupuesto_mensual", "dependientes", "fumador",
    "ocupacion_riesgo", "objetivo", "aseguradora", "cobertura_sugerida",
    "prima_estimada", "plan_recomendado", "perfil_riesgo", "estatus", "completion_seconds",
]
LOG_COLUMNS = [
    "id", "timestamp", "action_type", "status", "detail", "duration_ms",
    "user_name", "source", "metadata_json",
]


def is_supabase_enabled() -> bool:
    return DATA_BACKEND == "supabase" and bool(SUPABASE_URL) and bool(SUPABASE_KEY)


def is_mongo_enabled() -> bool:
    return DATA_BACKEND == "mongo" and bool(MONGODB_URI) and bool(MONGODB_DB)


@st.cache_resource
def get_mongo_client() -> MongoClient:
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)


def mongo_db():
    return get_mongo_client()[MONGODB_DB]


def mongo_leads_collection():
    return mongo_db()[MONGODB_LEADS_COLLECTION]


def mongo_logs_collection():
    return mongo_db()[MONGODB_LOGS_COLLECTION]


def mongo_next_id(collection) -> int:
    doc = collection.find_one(sort=[("id", DESCENDING)], projection={"id": 1})
    if not doc or "id" not in doc:
        return 1
    try:
        return int(doc["id"]) + 1
    except Exception:
        return 1


def mongo_sanitize_record(doc: dict) -> dict:
    if not doc:
        return {}
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def supabase_table_url(table_name: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table_name}"


def safe_df(records: list[dict] | None, columns: list[str]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(records)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df[columns]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    if is_supabase_enabled() or is_mongo_enabled():
        return
    with closing(get_connection()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                nombre TEXT NOT NULL,
                correo TEXT NOT NULL,
                telefono TEXT NOT NULL,
                edad INTEGER NOT NULL,
                ingreso_mensual REAL NOT NULL,
                presupuesto_mensual REAL NOT NULL,
                dependientes INTEGER NOT NULL,
                fumador TEXT NOT NULL,
                ocupacion_riesgo TEXT NOT NULL,
                objetivo TEXT NOT NULL,
                aseguradora TEXT NOT NULL,
                cobertura_sugerida REAL NOT NULL,
                prima_estimada REAL NOT NULL,
                plan_recomendado TEXT NOT NULL,
                perfil_riesgo TEXT NOT NULL,
                estatus TEXT NOT NULL,
                completion_seconds REAL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                duration_ms REAL DEFAULT 0,
                user_name TEXT,
                source TEXT DEFAULT 'streamlit',
                metadata_json TEXT
            )
            """
        )
        conn.commit()


def log_event(action_type: str, status: str, detail: str, duration_ms: float = 0, user_name: str = "", metadata: dict | None = None) -> None:
    if not ENABLE_LOGGING:
        return
    payload = {
        "timestamp": now_str(),
        "action_type": action_type,
        "status": status,
        "detail": detail,
        "duration_ms": float(duration_ms),
        "user_name": user_name,
        "source": APP_ENV,
        "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
    }

    if is_supabase_enabled():
        try:
            response = requests.post(
                supabase_table_url(SUPABASE_LOGS_TABLE),
                headers=supabase_headers(),
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
        except Exception:
            return
        return

    if is_mongo_enabled():
        try:
            collection = mongo_logs_collection()
            payload_to_insert = dict(payload)
            payload_to_insert["id"] = mongo_next_id(collection)
            collection.insert_one(payload_to_insert)
        except Exception:
            return
        return

    with closing(get_connection()) as conn:
        conn.execute(
            """
            INSERT INTO usage_logs (timestamp, action_type, status, detail, duration_ms, user_name, source, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["timestamp"],
                payload["action_type"],
                payload["status"],
                payload["detail"],
                payload["duration_ms"],
                payload["user_name"],
                payload["source"],
                payload["metadata_json"],
            ),
        )
        conn.commit()


def load_leads() -> pd.DataFrame:
    if is_supabase_enabled():
        try:
            response = requests.get(
                supabase_table_url(SUPABASE_LEADS_TABLE),
                headers=supabase_headers(),
                params={"select": "*", "order": "id.desc"},
                timeout=20,
            )
            response.raise_for_status()
            return safe_df(response.json(), LEADS_COLUMNS)
        except Exception as exc:
            st.warning(f"No se pudieron cargar leads desde Supabase: {exc}")
            return pd.DataFrame(columns=LEADS_COLUMNS)
    if is_mongo_enabled():
        try:
            rows = [mongo_sanitize_record(x) for x in mongo_leads_collection().find({}, {"_id": 0}).sort("id", DESCENDING)]
            return safe_df(rows, LEADS_COLUMNS)
        except Exception as exc:
            st.warning(f"No se pudieron cargar solicitudes desde MongoDB: {exc}")
            return pd.DataFrame(columns=LEADS_COLUMNS)

    with closing(get_connection()) as conn:
        return pd.read_sql_query("SELECT * FROM leads ORDER BY id DESC", conn)


def load_logs() -> pd.DataFrame:
    if is_supabase_enabled():
        try:
            response = requests.get(
                supabase_table_url(SUPABASE_LOGS_TABLE),
                headers=supabase_headers(),
                params={"select": "*", "order": "id.desc"},
                timeout=20,
            )
            response.raise_for_status()
            return safe_df(response.json(), LOG_COLUMNS)
        except Exception as exc:
            st.warning(f"No se pudieron cargar logs desde Supabase: {exc}")
            return pd.DataFrame(columns=LOG_COLUMNS)
    if is_mongo_enabled():
        try:
            rows = [mongo_sanitize_record(x) for x in mongo_logs_collection().find({}, {"_id": 0}).sort("id", DESCENDING)]
            return safe_df(rows, LOG_COLUMNS)
        except Exception as exc:
            st.warning(f"No se pudieron cargar logs desde MongoDB: {exc}")
            return pd.DataFrame(columns=LOG_COLUMNS)

    with closing(get_connection()) as conn:
        return pd.read_sql_query("SELECT * FROM usage_logs ORDER BY id DESC", conn)


def save_lead(row: dict) -> int:
    if is_supabase_enabled():
        response = requests.post(
            supabase_table_url(SUPABASE_LEADS_TABLE),
            headers=supabase_headers(),
            json=row,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return int(data[0]["id"]) if data and "id" in data[0] else 0
    if is_mongo_enabled():
        collection = mongo_leads_collection()
        row = dict(row)
        row["id"] = mongo_next_id(collection)
        collection.insert_one(row)
        return int(row["id"])

    with closing(get_connection()) as conn:
        cursor = conn.execute(
            """
            INSERT INTO leads (
                created_at, updated_at, nombre, correo, telefono, edad, ingreso_mensual,
                presupuesto_mensual, dependientes, fumador, ocupacion_riesgo, objetivo,
                aseguradora, cobertura_sugerida, prima_estimada, plan_recomendado,
                perfil_riesgo, estatus, completion_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["created_at"],
                row["updated_at"],
                row["nombre"],
                row["correo"],
                row["telefono"],
                row["edad"],
                row["ingreso_mensual"],
                row["presupuesto_mensual"],
                row["dependientes"],
                row["fumador"],
                row["ocupacion_riesgo"],
                row["objetivo"],
                row["aseguradora"],
                row["cobertura_sugerida"],
                row["prima_estimada"],
                row["plan_recomendado"],
                row["perfil_riesgo"],
                row["estatus"],
                row.get("completion_seconds", 0),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def update_lead_status(lead_id: int, new_status: str) -> None:
    if is_supabase_enabled():
        response = requests.patch(
            supabase_table_url(SUPABASE_LEADS_TABLE),
            headers=supabase_headers(),
            params={"id": f"eq.{lead_id}"},
            json={"estatus": new_status, "updated_at": now_str()},
            timeout=20,
        )
        response.raise_for_status()
        return
    if is_mongo_enabled():
        mongo_leads_collection().update_one({"id": int(lead_id)}, {"$set": {"estatus": new_status, "updated_at": now_str()}})
        return

    with closing(get_connection()) as conn:
        conn.execute(
            "UPDATE leads SET estatus = ?, updated_at = ? WHERE id = ?",
            (new_status, now_str(), lead_id),
        )
        conn.commit()


def delete_lead(lead_id: int) -> None:
    if is_supabase_enabled():
        response = requests.delete(
            supabase_table_url(SUPABASE_LEADS_TABLE),
            headers=supabase_headers(),
            params={"id": f"eq.{lead_id}"},
            timeout=20,
        )
        response.raise_for_status()
        return
    if is_mongo_enabled():
        mongo_leads_collection().delete_one({"id": int(lead_id)})
        return

    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.commit()


def export_leads_csv() -> bytes:
    df = load_leads()
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    with open(CSV_EXPORT_FILE, "wb") as f:
        f.write(csv_bytes)
    return csv_bytes


def backup_export_bytes() -> bytes:
    if is_supabase_enabled() or is_mongo_enabled():
        leads_df = load_leads()
        logs_df = load_logs()
        payload = {
            "backend": "supabase" if is_supabase_enabled() else "mongo",
            "exported_at": now_str(),
            "leads": leads_df.to_dict(orient="records"),
            "usage_logs": logs_df.to_dict(orient="records"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    if not os.path.exists(DB_FILE):
        return b""
    with open(DB_FILE, "rb") as f:
        return f.read()


def backup_export_filename() -> str:
    if is_supabase_enabled():
        return "asegura_vida_supabase_backup.json"
    if is_mongo_enabled():
        return "asegura_vida_mongo_backup.json"
    return "asegura_vida_app.db"


def backup_export_mime() -> str:
    return "application/json" if is_supabase_enabled() else "application/octet-stream"


# =========================
# Utilidades de negocio
# =========================
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def money(value: float) -> str:
    return f"${value:,.0f} MXN"


def digits_only(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def validate_contact(nombre: str, telefono: str, correo: str) -> list[str]:
    errors: list[str] = []
    if len(nombre.strip()) < 3:
        errors.append("Escribe un nombre completo válido.")
    if len(digits_only(telefono)) < 10:
        errors.append("Escribe un teléfono válido de al menos 10 dígitos.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", correo.strip()):
        errors.append("Escribe un correo electrónico válido.")
    return errors


def validate_quote_inputs(edad: int, ingreso: float, presupuesto: float, dependientes: int) -> list[str]:
    errors: list[str] = []
    if edad < 18 or edad > 65:
        errors.append("La edad debe estar entre 18 y 65 años para esta simulación.")
    if ingreso < 10000:
        errors.append("El ingreso mensual debe ser de al menos $10,000 MXN.")
    if presupuesto < 500:
        errors.append("El presupuesto mensual mínimo es de $500 MXN.")
    if dependientes < 0:
        errors.append("El número de dependientes no puede ser negativo.")
    return errors


def risk_level(fumador: str, ocupacion_riesgo: str, edad: int) -> str:
    score = 0
    if fumador == "Sí":
        score += 2
    if ocupacion_riesgo == "Sí":
        score += 2
    if edad >= 45:
        score += 1
    if score <= 1:
        return "Riesgo bajo"
    if score <= 3:
        return "Riesgo medio"
    return "Riesgo alto"


def suggested_coverage(ingreso_mensual: float, dependientes: int, edad: int, fumador: str, ocupacion_riesgo: str) -> float:
    years = 5
    if dependientes >= 2:
        years = 8
    if dependientes >= 4:
        years = 10
    base = ingreso_mensual * 12 * years
    extra_dependientes = dependientes * 150_000
    funeral_support = 80_000
    risk_extra = 0
    if fumador == "Sí":
        risk_extra += 120_000
    if ocupacion_riesgo == "Sí":
        risk_extra += 180_000
    if edad >= 50:
        risk_extra += 100_000
    return max(500_000, base + extra_dependientes + funeral_support + risk_extra)


def estimated_premium(cobertura: float, edad: int, fumador: str, ocupacion_riesgo: str, factor: float) -> float:
    base_rate = 0.00085 + (max(edad - 18, 0) * 0.00004)
    if fumador == "Sí":
        base_rate += 0.00035
    if ocupacion_riesgo == "Sí":
        base_rate += 0.00025
    return cobertura * base_rate * factor


def recommend_plan(cobertura: float, prima: float, presupuesto: float) -> str:
    if cobertura >= 2_500_000 and prima <= presupuesto * 1.15:
        return "Protección Familiar Amplia"
    if cobertura >= 1_200_000:
        return "Protección Familiar Balanceada"
    return "Protección Base Individual"


def make_whatsapp_link(nombre: str, plan: str, aseguradora: str, cobertura: float, prima: float, correo: str, telefono: str) -> str:
    text = (
        f"Hola, soy {nombre}. Quiero continuar con mi cotización de seguro de vida.%0A"
        f"Plan recomendado: {plan}%0A"
        f"Aseguradora sugerida: {aseguradora}%0A"
        f"Cobertura sugerida: {money(cobertura)}%0A"
        f"Prima estimada mensual: {money(prima)}%0A"
        f"Correo: {correo}%0A"
        f"Teléfono: {telefono}"
    )
    return f"https://wa.me/{WHATSAPP_NUMBER}?text={quote(text, safe='%0A')}"


def compute_live_metrics(leads_df: pd.DataFrame, logs_df: pd.DataFrame) -> dict:
    metrics = {
        "cotizaciones": 0,
        "solicituds": 0,
        "errores": 0,
        "tiempo_promedio_ms": 0,
        "whatsapp": 0,
    }
    if not logs_df.empty:
        metrics["cotizaciones"] = int((logs_df["action_type"] == "quote_generated").sum())
        metrics["errores"] = int((logs_df["status"] == "error").sum())
        metrics["whatsapp"] = int((logs_df["action_type"] == "whatsapp_opened").sum())
        duration_df = logs_df[logs_df["duration_ms"] > 0]
        if not duration_df.empty:
            metrics["tiempo_promedio_ms"] = float(duration_df["duration_ms"].mean())
    if not leads_df.empty:
        metrics["solicituds"] = int(len(leads_df))
    return metrics


# =========================
# UI y estilo
# =========================
def inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        html, body, [class*="css"] {font-family: 'Inter', sans-serif;}
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(59,130,246,.10), transparent 26%),
                radial-gradient(circle at top right, rgba(14,165,233,.10), transparent 24%),
                linear-gradient(180deg, #f8fbff 0%, #eef4ff 46%, #f8fbff 100%);
            color: #0f172a;
        }
        .block-container {padding-top: 1.2rem; padding-bottom: 2.8rem; max-width: 1240px;}
        [data-testid="stHeader"] {background: rgba(248,251,255,0.88);}
        [data-testid="stSidebar"] {background: linear-gradient(180deg, #0f172a 0%, #111827 100%);}
        [data-testid="stSidebar"] * {color: #e2e8f0 !important;}

        .topbar {display:flex; justify-content:space-between; align-items:center; gap:20px; background:rgba(255,255,255,0.92); border:1px solid #e2e8f0; border-radius:24px; padding:14px 18px; box-shadow:0 18px 40px rgba(15,23,42,0.06);}
        .brand {display:flex; gap:12px; align-items:center; font-weight:800; color:#1d4ed8; font-size:1.18rem;}
        .brand-badge {width:44px; height:44px; border-radius:14px; background:linear-gradient(135deg,#dbeafe,#bfdbfe); display:flex; align-items:center; justify-content:center; font-size:1.1rem; box-shadow: inset 0 1px 0 rgba(255,255,255,.9);}
        .brand-sub {color:#64748b; font-weight:600;}

        .hero-card {background: linear-gradient(135deg, rgba(255,255,255,.98) 0%, rgba(248,251,255,.98) 100%); border:1px solid #dbe4f2; border-radius:34px; padding:38px; box-shadow:0 24px 60px rgba(15,23,42,0.07);}
        .eyebrow {display:inline-flex; align-items:center; gap:8px; background:#eff6ff; border:1px solid #dbeafe; color:#2563eb; font-size:.86rem; font-weight:800; padding:9px 14px; border-radius:999px;}
        .hero-title {font-size:3.3rem; line-height:1.02; font-weight:800; margin:18px 0 14px 0; letter-spacing:-0.03em;}
        .hero-title span {background: linear-gradient(90deg, #2563eb 0%, #60a5fa 100%); -webkit-background-clip:text; -webkit-text-fill-color:transparent;}
        .hero-sub {font-size:1.02rem; line-height:1.82; color:#475569; max-width:680px;}
        .pill-row {display:flex; gap:12px; flex-wrap:wrap; margin-top:18px;}
        .pill {background:white; border:1px solid #e2e8f0; padding:10px 14px; border-radius:999px; font-weight:700; color:#334155; box-shadow:0 6px 18px rgba(15,23,42,0.04);}
        .panel {background:linear-gradient(180deg, #ffffff 0%, #f8fbff 100%); border:1px solid #e2e8f0; border-radius:28px; padding:24px; box-shadow:0 18px 48px rgba(15,23,42,0.06);}
        .section-title {text-align:center; font-size:2.1rem; font-weight:800; margin-top:8px; margin-bottom:8px; letter-spacing:-0.03em;}
        .section-sub {text-align:center; color:#64748b; max-width:790px; margin:0 auto 24px auto; line-height:1.78;}

        .result-card {background:linear-gradient(180deg, #0f172a 0%, #111827 100%); color:white; border-radius:28px; padding:30px; min-height:100%; box-shadow:0 22px 46px rgba(15,23,42,0.18); border:1px solid rgba(255,255,255,.04);}
        .result-logo-wrap {height:48px; display:flex; align-items:center; margin-bottom:18px;}
        .result-logo-img {max-height:38px; max-width:170px; object-fit:contain; object-position:left center; filter: brightness(1.05);}
        .result-label {font-size:.76rem; letter-spacing:.12em; text-transform:uppercase; color:#93c5fd; font-weight:800;}
        .big-money {font-size:2.3rem; font-weight:800; color:#4ade80; margin:8px 0 10px 0;}
        .soft {color:#cbd5e1; line-height:1.72;}

        .insurer-card {background:linear-gradient(180deg, rgba(255,255,255,.98) 0%, rgba(248,251,255,.95) 100%); border:1px solid #dde6f3; border-radius:26px; padding:24px; min-height:340px; box-shadow:0 14px 34px rgba(15,23,42,0.055); transition:transform .2s ease, box-shadow .2s ease;}
        .insurer-card:hover {transform: translateY(-2px); box-shadow:0 18px 40px rgba(15,23,42,0.08);}
        .insurer-logo-wrap {height:54px; display:flex; align-items:center; margin-bottom:16px;}
        .insurer-logo-img {max-height:42px; max-width:160px; object-fit:contain; object-position:left center;}
        .insurer-name {font-size:1.26rem; font-weight:800; color:#0f172a; margin-bottom:6px;}
        .insurer-tag {display:inline-block; background:#e8eef8; color:#334155; font-size:.75rem; font-weight:800; padding:7px 12px; border-radius:999px; margin-bottom:12px;}
        .mini-note {background:#eff6ff; color:#1e3a8a; border:1px solid #dbeafe; border-radius:18px; padding:14px 16px; font-size:.95rem; line-height:1.6;}
        .story-card {background:white; border:1px solid #e2e8f0; border-radius:20px; padding:16px 18px; margin-bottom:10px; box-shadow:0 8px 24px rgba(15,23,42,0.04);}
        .footer {background:#0f172a; color:white; border-radius:28px; padding:40px 28px; text-align:center;}
        .footer p {color:#94a3b8;}

        .stTextInput label, .stNumberInput label, .stSelectbox label, .stTextArea label, .stDateInput label, .stTimeInput label, div[data-testid="stWidgetLabel"] label {
            color:#0f172a !important; font-weight:700 !important; opacity:1 !important;
        }
        div[data-baseweb="input"] > div, div[data-baseweb="select"] > div {
            background:white !important; color:#0f172a !important; border-radius:14px !important; border:1px solid #cbd5e1 !important;
        }
        div[data-baseweb="input"] > div:focus-within, div[data-baseweb="select"] > div:focus-within {
            border-color:#60a5fa !important; box-shadow:0 0 0 4px rgba(96,165,250,.15) !important;
        }
        input, textarea {color:#0f172a !important; -webkit-text-fill-color:#0f172a !important; opacity:1 !important;}
        [data-testid="stNumberInput"] input {color:#0f172a !important; -webkit-text-fill-color:#0f172a !important; opacity:1 !important;}
        [data-testid="stNumberInputStepDown"], [data-testid="stNumberInputStepUp"] {
            background:#1f2937 !important; border:none !important;
        }
        [data-testid="stNumberInputStepDown"] *, [data-testid="stNumberInputStepUp"] * {color:white !important;}
        .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
            border-radius:14px; font-weight:800; border:1px solid #dbeafe; padding:.72rem 1rem; box-shadow:0 8px 18px rgba(37,99,235,.08);
        }
        .stFormSubmitButton > button[kind="primary"], .stButton > button[kind="primary"] {
            background:linear-gradient(90deg,#2563eb,#3b82f6) !important; color:white !important; border:none !important;
        }
        .stAlert {border-radius:18px;}
        [data-testid="metric-container"] {background:white; border:1px solid #e2e8f0; border-radius:20px; padding:12px 16px; box-shadow:0 8px 22px rgba(15,23,42,0.04);}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header() -> None:
    st.markdown(
        """
        <div class="topbar">
            <div class="brand"><div class="brand-badge">🛡️</div> Asegura+ Vida</div>
            <div class="brand-sub">Prototipo funcional de seguro de vida · cotización, seguimiento y métricas</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero() -> None:
    c1, c2 = st.columns([1.35, 1], gap="large")
    with c1:
        st.markdown(
            """
            <div class="hero-card">
                <div class="eyebrow">Seguro de vida individual y familiar</div>
                <div class="hero-title">Protege hoy a quienes <span>dependen de ti</span></div>
                <div class="hero-sub">
                    Esta app está enfocada solo en seguro de vida. Ayuda a perfilar al solicitud,
                    calcular una cobertura sugerida, estimar una prima mensual y registrar el lead
                    para seguimiento comercial sin perder trazabilidad.
                </div>
                <div class="pill-row">
                    <div class="pill">Protección familiar</div>
                    <div class="pill">Cobertura sugerida</div>
                    <div class="pill">Prima estimada</div>
                    <div class="pill">Logs y métricas</div>
                    <div class="pill">Registro y backup</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            """
            <div class="panel" style="height:100%;">
                <div class="eyebrow">Checklist MVP</div>
                <h3 style="margin:14px 0 8px 0;">Lo que ya cubre esta versión</h3>
                <div style="color:#475569; line-height:1.82;">
                    • Cotizador funcional con validaciones.<br>
                    • Persistencia en SQLite y exportación de respaldo.<br>
                    • Registro CRUD básico de solicituds.<br>
                    • Logs de éxito y fallo para documentar pruebas.<br>
                    • Dashboard de métricas en vivo para evidencia.
                </div>
                <div class="mini-note" style="margin-top:16px;">La intención es que puedas enseñar no solo la interfaz, sino también cómo pasó el checklist técnico y funcional.</div>
                <div style="margin-top:18px; display:flex; justify-content:space-between; align-items:center; gap:16px; opacity:.8; filter:grayscale(100%);">
                    <img src="https://upload.wikimedia.org/wikipedia/commons/c/c6/MetLife_logo.svg" style="height:20px;max-width:100%;object-fit:contain;" />
                    <img src="https://upload.wikimedia.org/wikipedia/commons/2/26/GNP_Seguros_logo.svg" style="height:28px;max-width:100%;object-fit:contain;" />
                    <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/HD_transparent_picture.png/1px-HD_transparent_picture.png" style="height:30px;max-width:100%;object-fit:contain;" />
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_user_stories() -> None:
    st.markdown('<div class="section-title">User stories del prototipo</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Estas historias se alinean con las features principales que te van a pedir justificar en el checklist.</div>', unsafe_allow_html=True)
    cols = st.columns(2, gap="large")
    for idx, story in enumerate(USER_STORIES):
        with cols[idx % 2]:
            st.markdown(f'<div class="story-card">{story}</div>', unsafe_allow_html=True)


def render_insurers() -> None:
    st.markdown('<div class="section-title">Aseguradoras sugeridas</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Se restauró la presentación visual y se dejó una sección más comercial, más cercana a la versión que te gustaba.</div>', unsafe_allow_html=True)
    cols = st.columns(3, gap="large")
    for col, insurer in zip(cols, INSURERS):
        with col:
            logo_html = f'<img src="{insurer["logo"]}" class="insurer-logo-img" />' if insurer["name"] != "Insignia Life" else '<div style="font-size:2rem; font-weight:900; color:#7c3aed; letter-spacing:-.03em;">Insignia <span style="color:#0f172a;">Life</span></div>'
            st.markdown(
                f"""
                <div class="insurer-card">
                    <div class="insurer-logo-wrap">{logo_html}</div>
                    <div class="insurer-name">{insurer['name']}</div>
                    <div class="insurer-tag">{insurer['tag']}</div>
                    <div style="color:#475569; line-height:1.85; min-height:94px;">{insurer['description']}</div>
                    <div style="margin-top:14px; color:#0f172a; font-weight:800;">Enfoque comercial</div>
                    <div style="margin-top:8px; color:#334155; line-height:1.74;">Ideal para presentar una alternativa inicial clara, confiable y alineada a protección familiar.</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


# =========================
# Secciones funcionales
# =========================
def quote_tab() -> None:
    st.markdown('<div class="section-title">Cotizador inicial de seguro de vida</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Volví a poner un aproximado rápido desde el inicio. Primero puedes estimar con edad y presupuesto; después, si te interesa, completas tus datos para guardar la solicitud.</div>', unsafe_allow_html=True)

    if "quick_age" not in st.session_state:
        st.session_state["quick_age"] = 30
    if "quick_budget" not in st.session_state:
        st.session_state["quick_budget"] = 2000
    if "quick_insurer" not in st.session_state:
        st.session_state["quick_insurer"] = INSURERS[0]["name"]

    top_left, top_right = st.columns([0.95, 1.05], gap="large")
    with top_left:
        st.markdown("### Aproximado inmediato")
        st.caption("Solo con estos datos ya puedes darte una idea del nivel de protección.")
        q1, q2 = st.columns(2)
        with q1:
            quick_age = st.number_input("Edad para simulación rápida", min_value=18, max_value=65, value=st.session_state["quick_age"], step=1, key="quick_age_input")
        with q2:
            quick_budget = st.number_input("Presupuesto mensual estimado", min_value=500, max_value=50000, value=st.session_state["quick_budget"], step=100, key="quick_budget_input")
        quick_insurer = st.selectbox("Aseguradora de referencia", [x["name"] for x in INSURERS], index=[x["name"] for x in INSURERS].index(st.session_state["quick_insurer"]), key="quick_insurer_select")

        st.markdown("### Solicita tu propuesta completa")
        st.caption("Aquí ya capturas tus datos para guardar la solicitud y abrir seguimiento comercial.")
        with st.form("quote_form", clear_on_submit=False):
            nombre = st.text_input("Nombre completo", placeholder="Ej. Juan Pérez")
            correo = st.text_input("Correo electrónico", placeholder="correo@ejemplo.com")
            telefono = st.text_input("Teléfono", placeholder="55 1234 5678")

            c1, c2 = st.columns(2)
            with c1:
                edad = st.number_input("Edad", min_value=18, max_value=65, value=int(quick_age), step=1)
                ingreso = st.number_input("Ingreso mensual", min_value=10000, max_value=500000, value=35000, step=1000)
            with c2:
                presupuesto = st.number_input("Presupuesto mensual", min_value=500, max_value=50000, value=int(quick_budget), step=100)
                dependientes = st.number_input("Número de dependientes", min_value=0, max_value=10, value=0, step=1)

            c3, c4 = st.columns(2)
            with c3:
                fumador = st.selectbox("¿Es fumador?", ["No", "Sí"])
                objetivo = st.selectbox(
                    "Objetivo principal",
                    [
                        "Protección familiar",
                        "Respaldo para hijos o dependientes",
                        "Cobertura por deudas o hipoteca",
                        "Protección individual",
                    ],
                )
            with c4:
                ocupacion_riesgo = st.selectbox("¿Tiene ocupación de riesgo?", ["No", "Sí"])
                aseguradora = st.selectbox("Aseguradora sugerida", [x["name"] for x in INSURERS], index=[x["name"] for x in INSURERS].index(quick_insurer))

            submitted = st.form_submit_button("Solicitar propuesta completa", type="primary")

        if submitted:
            t0 = time.perf_counter()
            errors = []
            errors.extend(validate_contact(nombre, telefono, correo))
            errors.extend(validate_quote_inputs(int(edad), float(ingreso), float(presupuesto), int(dependientes)))
            duration_ms = (time.perf_counter() - t0) * 1000

            if errors:
                for err in errors:
                    st.error(err)
                log_event(
                    action_type="quote_generated",
                    status="error",
                    detail="Validación fallida en cotización",
                    duration_ms=duration_ms,
                    user_name=nombre,
                    metadata={"errors": errors},
                )
                st.session_state["quote"] = None
            else:
                insurer = next(item for item in INSURERS if item["name"] == aseguradora)
                cobertura = suggested_coverage(float(ingreso), int(dependientes), int(edad), fumador, ocupacion_riesgo)
                prima = estimated_premium(cobertura, int(edad), fumador, ocupacion_riesgo, insurer["factor"])
                plan = recommend_plan(cobertura, prima, float(presupuesto))
                perfil = risk_level(fumador, ocupacion_riesgo, int(edad))
                completion_seconds = round((time.perf_counter() - st.session_state.get("app_start_perf", time.perf_counter())), 2)
                st.session_state["quote"] = {
                    "nombre": nombre.strip(),
                    "correo": correo.strip(),
                    "telefono": telefono.strip(),
                    "edad": int(edad),
                    "ingreso": float(ingreso),
                    "presupuesto": float(presupuesto),
                    "dependientes": int(dependientes),
                    "fumador": fumador,
                    "ocupacion_riesgo": ocupacion_riesgo,
                    "objetivo": objetivo,
                    "aseguradora": aseguradora,
                    "cobertura": cobertura,
                    "prima": prima,
                    "plan": plan,
                    "perfil": perfil,
                    "completion_seconds": completion_seconds,
                    "duration_ms": round(duration_ms, 2),
                }
                log_event(
                    action_type="quote_generated",
                    status="success",
                    detail=f"Cotización generada: {plan}",
                    duration_ms=duration_ms,
                    user_name=nombre,
                    metadata={"plan": plan, "aseguradora": aseguradora},
                )
                st.success("Propuesta completa calculada correctamente.")
                if duration_ms <= 2000:
                    st.info(f"Tiempo de respuesta: {duration_ms:.0f} ms. Dentro del benchmark objetivo.")
                else:
                    st.warning(f"Tiempo de respuesta: {duration_ms:.0f} ms. Supera el objetivo de 2 segundos.")

    with top_right:
        quick_insurer_obj = next(item for item in INSURERS if item["name"] == quick_insurer)
        quick_income = max(20000, quick_budget * 12)
        quick_dependents = 1 if quick_budget >= 1500 else 0
        quick_coverage = max(500000, round(quick_budget * 1000 + quick_age * 15000, -3))
        quick_premium = estimated_premium(quick_coverage, int(quick_age), "No", "No", quick_insurer_obj["factor"])
        quick_plan = recommend_plan(quick_coverage, quick_premium, float(quick_budget))

        def result_logo_html(insurer: dict) -> str:
            if insurer["name"] == "Insignia Life":
                return '<div style="font-size:1.55rem; font-weight:900; color:#a78bfa; letter-spacing:-.03em;">Insignia <span style="color:white;">Life</span></div>'
            return f'<img src="{insurer["logo"]}" class="result-logo-img" />'

        quote = st.session_state.get("quote")
        if quote:
            st.markdown(
                f"""
                <div class="result-card">
                    <div class="result-logo-wrap">{result_logo_html(next(item for item in INSURERS if item["name"] == quote['aseguradora']))}</div>
                    <div class="result-label">Plan recomendado</div>
                    <h2 style="margin:8px 0 4px 0;">{quote['plan']}</h2>
                    <div class="soft">{quote['aseguradora']} · {quote['perfil']}</div>
                    <div style="height:14px"></div>
                    <div class="result-label">Cobertura sugerida</div>
                    <div class="big-money">{money(quote['cobertura'])}</div>
                    <div class="result-label">Prima mensual estimada</div>
                    <div style="font-size:1.7rem; font-weight:800; color:white; margin:8px 0 18px 0;">{money(quote['prima'])}</div>
                    <div class="soft">Esta propuesta ya considera ingreso, dependientes y factores de riesgo. Sirve como base para seguimiento comercial y para guardar el solicitud.</div>
                    <div style="border-top:1px solid rgba(148,163,184,0.22); margin:18px 0;"></div>
                    <div class="soft"><strong style="color:white;">Objetivo:</strong> {quote['objetivo']}</div>
                    <div class="soft"><strong style="color:white;">Presupuesto mensual:</strong> {money(quote['presupuesto'])}</div>
                    <div class="soft"><strong style="color:white;">Tiempo del flujo:</strong> {quote['completion_seconds']:.1f} s</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            a, b = st.columns(2)
            with a:
                if st.button("Guardar solicitud", type="primary", use_container_width=True):
                    row = {
                        "created_at": now_str(),
                        "updated_at": now_str(),
                        "nombre": quote["nombre"],
                        "correo": quote["correo"],
                        "telefono": quote["telefono"],
                        "edad": quote["edad"],
                        "ingreso_mensual": quote["ingreso"],
                        "presupuesto_mensual": quote["presupuesto"],
                        "dependientes": quote["dependientes"],
                        "fumador": quote["fumador"],
                        "ocupacion_riesgo": quote["ocupacion_riesgo"],
                        "objetivo": quote["objetivo"],
                        "aseguradora": quote["aseguradora"],
                        "cobertura_sugerida": round(quote["cobertura"], 2),
                        "prima_estimada": round(quote["prima"], 2),
                        "plan_recomendado": quote["plan"],
                        "perfil_riesgo": quote["perfil"],
                        "estatus": "Nueva solicitud",
                        "completion_seconds": quote["completion_seconds"],
                    }
                    t0 = time.perf_counter()
                    lead_id = save_lead(row)
                    duration_ms = (time.perf_counter() - t0) * 1000
                    log_event(
                        action_type="request_saved",
                        status="success",
                        detail=f"Solicitud guardada #{lead_id}",
                        duration_ms=duration_ms,
                        user_name=quote["nombre"],
                        metadata={"lead_id": lead_id},
                    )
                    st.success("Solicitud guardada correctamente.")
            with b:
                if st.button("Continuar por WhatsApp", use_container_width=True):
                    wa = make_whatsapp_link(
                        quote["nombre"],
                        quote["plan"],
                        quote["aseguradora"],
                        quote["cobertura"],
                        quote["prima"],
                        quote["correo"],
                        quote["telefono"],
                    )
                    log_event(
                        action_type="whatsapp_opened",
                        status="success",
                        detail="Usuario enviado a WhatsApp",
                        duration_ms=0,
                        user_name=quote["nombre"],
                        metadata={"plan": quote["plan"]},
                    )
                    st.link_button("Abrir WhatsApp", wa, use_container_width=True)
        else:
            st.markdown(
                f"""
                <div class="result-card">
                    <div class="result-logo-wrap">{result_logo_html(quick_insurer_obj)}</div>
                    <div class="result-label">Aproximado inmediato</div>
                    <h2 style="margin:8px 0 4px 0;">{quick_plan}</h2>
                    <div class="soft">{quick_insurer} · simulación rápida</div>
                    <div style="height:14px"></div>
                    <div class="result-label">Cobertura aproximada</div>
                    <div class="big-money">{money(quick_coverage)}</div>
                    <div class="result-label">Prima mensual estimada</div>
                    <div style="font-size:1.7rem; font-weight:800; color:white; margin:8px 0 18px 0;">{money(quick_premium)}</div>
                    <div class="soft">Este cálculo rápido aparece desde el principio, solo con edad y presupuesto. Cuando completes la solicitud, la propuesta se recalcula con información más completa.</div>
                    <div style="border-top:1px solid rgba(148,163,184,0.22); margin:18px 0;"></div>
                    <div class="soft"><strong style="color:white;">Edad:</strong> {quick_age} años</div>
                    <div class="soft"><strong style="color:white;">Presupuesto mensual:</strong> {money(quick_budget)}</div>
                    <div class="soft"><strong style="color:white;">Uso recomendado:</strong> primer acercamiento visual con el solicitud</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('<div class="panel" style="margin-top:16px;">La solicitud completa queda debajo y solo se usa cuando ya quieres guardar datos del solicitud o abrir seguimiento.</div>', unsafe_allow_html=True)


def registry_tab() -> None:
    st.markdown('<div class="section-title">Registro de solicituds</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Aquí cumples persistencia, backup, CRUD básico y consistencia de datos para documentar el checklist.</div>', unsafe_allow_html=True)

    df = load_leads()
    if df.empty:
        st.info("Todavía no hay solicituds guardados. Genera una cotización y guarda un lead para probar el flujo completo.")
        return

    q1, q2, q3 = st.columns([1.2, 1, 1])
    with q1:
        busqueda = st.text_input("Buscar por nombre, correo o teléfono")
    with q2:
        estatus = st.selectbox("Filtrar por estatus", ["Todos"] + sorted(df["estatus"].dropna().unique().tolist()))
    with q3:
        aseguradora = st.selectbox("Filtrar por aseguradora", ["Todas"] + sorted(df["aseguradora"].dropna().unique().tolist()))

    view = df.copy()
    if busqueda.strip():
        mask = (
            view["nombre"].astype(str).str.contains(busqueda, case=False, na=False)
            | view["correo"].astype(str).str.contains(busqueda, case=False, na=False)
            | view["telefono"].astype(str).str.contains(busqueda, case=False, na=False)
        )
        view = view[mask]
    if estatus != "Todos":
        view = view[view["estatus"] == estatus]
    if aseguradora != "Todas":
        view = view[view["aseguradora"] == aseguradora]

    st.dataframe(view, use_container_width=True, hide_index=True)

    c1, c2 = st.columns([1.1, 0.9], gap="large")
    with c1:
        st.markdown("### Actualizar estatus")
        lead_options = [f"#{row['id']} · {row['nombre']}" for _, row in df.iterrows()]
        selected_label = st.selectbox("Selecciona una solicitud", lead_options)
        selected_id = int(selected_label.split("·")[0].replace("#", "").strip())
        new_status = st.selectbox(
            "Nuevo estatus",
            ["Nueva solicitud", "Contactado", "Documentación pendiente", "En análisis", "Emitido", "Cerrado no vendido"],
        )
        if st.button("Guardar cambio de estatus", type="primary"):
            t0 = time.perf_counter()
            update_lead_status(selected_id, new_status)
            duration_ms = (time.perf_counter() - t0) * 1000
            log_event(
                action_type="request_status_updated",
                status="success",
                detail=f"Lead #{selected_id} actualizado a {new_status}",
                duration_ms=duration_ms,
                metadata={"lead_id": selected_id, "new_status": new_status},
            )
            st.success("Estatus actualizado correctamente.")
            st.rerun()

    with c2:
        st.markdown("### Eliminar lead")
        delete_label = st.selectbox("Solicitud a eliminar", lead_options, key="delete_lead_select")
        delete_id_value = int(delete_label.split("·")[0].replace("#", "").strip())
        if st.button("Eliminar lead seleccionado"):
            t0 = time.perf_counter()
            delete_lead(delete_id_value)
            duration_ms = (time.perf_counter() - t0) * 1000
            log_event(
                action_type="request_deleted",
                status="success",
                detail=f"Lead #{delete_id_value} eliminado",
                duration_ms=duration_ms,
                metadata={"lead_id": delete_id_value},
            )
            st.success("Solicitud eliminada correctamente.")
            st.rerun()

    st.markdown("### Backup y exportación")
    b1, b2 = st.columns(2)
    with b1:
        st.download_button(
            "Descargar leads CSV",
            data=export_leads_csv(),
            file_name="leads_seguro_vida.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with b2:
        st.download_button(
            "Descargar respaldo backend",
            data=backup_export_bytes(),
            file_name=backup_export_filename(),
            mime=backup_export_mime(),
            use_container_width=True,
        )


def faq_tab() -> None:
    st.markdown('<div class="section-title">Preguntas frecuentes</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Se dejaron solo las preguntas que tú querías conservar para no ensuciar la experiencia.</div>', unsafe_allow_html=True)
    for question, answer in FAQS:
        with st.expander(question):
            st.write(answer)


def metrics_tab() -> None:
    st.markdown('<div class="section-title">Dashboard de logs y métricas</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Esta sección te sirve para demostrar logging, métricas, visualización live, captura de fallos y análisis inicial del uso del prototipo.</div>', unsafe_allow_html=True)

    password = st.text_input("Password de desarrollador", type="password")
    if password != DEV_PASSWORD:
        st.warning("Ingresa el password de desarrollador para ver esta sección.")
        return

    if st.button("Refresh métricas"):
        st.rerun()

    leads_df = load_leads()
    logs_df = load_logs()
    metrics = compute_live_metrics(leads_df, logs_df)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Cotizaciones", metrics["cotizaciones"])
    m2.metric("Prospectos", metrics["solicituds"])
    m3.metric("Errores", metrics["errores"])
    m4.metric("Resp. promedio", f"{metrics['tiempo_promedio_ms']:.0f} ms")
    m5.metric("WhatsApp", metrics["whatsapp"])

    st.markdown("### Métricas clave definidas")
    st.write(", ".join(KEY_METRICS_INFO))

    if logs_df.empty:
        st.info("Aún no hay logs. Genera algunas acciones dentro de la app para poblar esta sección.")
        return

    st.markdown("### Eventos por tipo")
    action_counts = logs_df["action_type"].value_counts().reset_index()
    action_counts.columns = ["Acción", "Total"]
    st.bar_chart(action_counts.set_index("Acción"))

    st.markdown("### Éxitos vs fallos")
    status_counts = logs_df["status"].value_counts().reset_index()
    status_counts.columns = ["Estado", "Total"]
    st.bar_chart(status_counts.set_index("Estado"))

    st.markdown("### Historial de logs")
    st.dataframe(logs_df, use_container_width=True, hide_index=True)

    if not leads_df.empty:
        st.markdown("### Análisis inicial")
        top_status = leads_df["estatus"].value_counts().idxmax()
        top_insurer = leads_df["aseguradora"].value_counts().idxmax()
        avg_completion = leads_df["completion_seconds"].mean() if "completion_seconds" in leads_df.columns else 0
        st.success(
            f"Insight rápido: el estatus más frecuente es '{top_status}', la aseguradora más cotizada es '{top_insurer}' y el tiempo promedio del flujo principal es {avg_completion:.1f} segundos."
        )


def testing_tab() -> None:
    st.markdown('<div class="section-title">Pruebas del prototipo</div>', unsafe_allow_html=True)
    st.markdown('<div class="section-sub">Esta sección te ayuda a demostrar input/output testing, workflow end-to-end, benchmarking y validación contra la entrevista.</div>', unsafe_allow_html=True)

    st.markdown("### Casos de prueba sugeridos")
    cases = pd.DataFrame(
        [
            ["Input válido", "Edad 30, ingreso 35,000, presupuesto 2,000", "Genera propuesta sin error"],
            ["Edad mínima", "18 años", "La app calcula sin crash"],
            ["Edad máxima", "65 años", "La app calcula sin crash"],
            ["Correo inválido", "juanmail.com", "Muestra mensaje user-friendly"],
            ["Teléfono corto", "123", "Muestra error"],
            ["Presupuesto mínimo", "$500", "Calcula propuesta"],
            ["Flujo completo", "Cotizar → guardar → actualizar estatus → exportar", "100% completado"],
        ],
        columns=["Prueba", "Entrada", "Resultado esperado"],
    )
    st.dataframe(cases, use_container_width=True, hide_index=True)

    st.markdown("### Validación contra entrevista")
    st.info(
        "Pain point resuelto: el familiar necesita una forma simple de explicar, cotizar y dar seguimiento inicial a un seguro de vida sin perder datos del solicitud."
    )

    st.markdown("### End-to-end workflow")
    st.write(
        "1) El usuario llena el formulario. 2) La app valida datos. 3) Genera cobertura y prima. 4) Guarda el lead. 5) Permite seguimiento por estatus y exportación."
    )


def render_footer() -> None:
    st.markdown(
        """
        <div class="footer">
            <h2 style="margin-top:0;">Asegura+ Vida</h2>
            <p>Prototipo final de seguro de vida con cotizador, registro de solicituds, métricas, logs y respaldos para documentar checklist.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# =========================
# App principal
# =========================
def main() -> None:
    init_db()
    inject_css()
    if "app_start_perf" not in st.session_state:
        st.session_state["app_start_perf"] = time.perf_counter()

    render_header()
    st.write("")
    render_hero()
    st.write("")
    render_user_stories()
    st.write("")
    render_insurers()
    st.write("")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "Cotizador",
            "Registro",
            "FAQ",
            "Logs y métricas",
            "Pruebas",
        ]
    )

    with tab1:
        quote_tab()
    with tab2:
        registry_tab()
    with tab3:
        faq_tab()
    with tab4:
        metrics_tab()
    with tab5:
        testing_tab()

    st.write("")
    render_footer()


if __name__ == "__main__":
    main()
