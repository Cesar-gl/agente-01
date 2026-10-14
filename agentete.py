import json
import os
import time
import schedule
import gspread
from google import genai
from google.genai import types

# ═════════════════════════════════════════════════════════════════════
# ⚙️ CONFIGURACIÓN DEL AGENTE Y GOOGLE FORMS / SHEETS
# ═════════════════════════════════════════════════════════════════════

# 1. Credenciales y Acceso
ARCHIVO_CREDENCIALES = "client_secret.json"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 2. Hoja de Cálculo vinculada a tu Google Form
# Puedes colocar el NOMBRE EXACTO del archivo en Drive, o el ID que aparece en la URL:
# https://docs.google.com/spreadsheets/d/TU_ID_AQUI/edit
NOMBRE_O_ID_HOJA = "Respuestas de Quejas (Base de Datos)"

# Nombre de la pestaña (deja None para usar la primera pestaña por defecto, ej: "Respuestas de formulario 1")
NOMBRE_PESTANA = None 

# 3. Mapeo de Columnas (1 = A, 2 = B, 3 = C, 4 = D, 5 = E, 6 = F, etc.)
# Ajusta según las columnas generadas por tu formulario:
COL_NOMBRE        = 2   # Columna B: Nombre del cliente (o contacto)
COL_COMENTARIO    = 3   # Columna C: Comentario / Queja / Respuesta del cliente
COL_CLASIFICACION = 4   # Columna D: Clasificación IA (Ventas, Soporte, etc.)
COL_SENTIMIENTO   = 5   # Columna E: Sentimiento IA (Positivo, Negativo, Neutro)
COL_EJECUTADO     = 6   # Columna F: Marca de procesamiento ("SI")

# 4. Parámetros de la IA y Ejecución
MODELO_GEMINI = "gemini-3.8-flash"  # Modelos recomendados: gemini-2.5-flash, gemini-2.0-flash, gemini-1.5-flash
INTERVALO_MINUTOS = 5
PAUSA_ENTRE_LLAMADAS_SEG = 12  # Para respetar los límites de cuota gratuita

# Categorías personalizables para tu negocio
CATEGORIAS = "Ventas, Soporte Técnico, Logística"
SENTIMIENTOS = "Positivo, Negativo, Neutro"

# ═════════════════════════════════════════════════════════════════════

cliente_gemini = None


def conectar_sheets():
    """
    Autentica con Google usando OAuth 2.0 (gspread)
    y abre la hoja vinculada al formulario.
    """
    try:
        if not os.path.exists(ARCHIVO_CREDENCIALES):
            print(f"❌ Error: No se encontró el archivo '{ARCHIVO_CREDENCIALES}'.")
            print("   Coloca el archivo de credenciales en esta misma carpeta.")
            return None

        # Autenticación con OAuth 2.0 (abrirá el navegador la primera vez)
        cliente = gspread.oauth(credentials_filename=ARCHIVO_CREDENCIALES)

        # Intento de apertura por ID (si tiene formato de ID) o por nombre exacto
        if len(NOMBRE_O_ID_HOJA) > 30 and "/" not in NOMBRE_O_ID_HOJA and " " not in NOMBRE_O_ID_HOJA:
            libro = cliente.open_by_key(NOMBRE_O_ID_HOJA)
        else:
            libro = cliente.open(NOMBRE_O_ID_HOJA)

        # Seleccionar pestaña específica o la primera hoja
        if NOMBRE_PESTANA:
            hoja = libro.worksheet(NOMBRE_PESTANA)
        else:
            hoja = libro.sheet1

        print(f"✅ Conectado exitosamente a la hoja: '{libro.title}' (Pestaña: '{hoja.title}')")
        return hoja

    except FileNotFoundError:
        print(f"❌ Error: Archivo '{ARCHIVO_CREDENCIALES}' no encontrado.")
        return None
    except gspread.exceptions.SpreadsheetNotFound:
        print(f"❌ Error: No se encontró la hoja '{NOMBRE_O_ID_HOJA}'.")
        print("   Verifica mayúsculas, tildes y espacios, o usa el ID de la URL.")
        return None
    except Exception as e:
        print(f"❌ Error inesperado al conectar con Google Sheets: {e}")
        return None


def analizar_con_gemini(comentario):
    """
    Envía el comentario del formulario a Gemini y retorna
    un diccionario con 'clasificación' y 'sentimiento'.
    """
    instruccion_sistema = (
        "Eres un analista de soporte y servicio al cliente de alta precisión. "
        "Tu única tarea es devolver una respuesta en formato JSON estricto, "
        "sin texto introductorio, sin explicaciones y sin bloques markdown. "
        'El JSON debe tener exactamente las claves "clasificación" y "sentimiento".'
    )

    instruccion_usuario = (
        f"Analiza el siguiente comentario recibido en el formulario:\n\n"
        f'"{comentario}"\n\n'
        f"Clasifícalo en una de estas categorías: {CATEGORIAS}.\n"
        f"Detecta el sentimiento: {SENTIMIENTOS}.\n"
        "Devuelve únicamente el objeto JSON."
    )

    respuesta = cliente_gemini.models.generate_content(
        model=MODELO_GEMINI,
        contents=instruccion_usuario,
        config=types.GenerateContentConfig(
            system_instruction=instruccion_sistema,
            response_mime_type="application/json",
            temperature=0.2
        ),
    )

    texto = respuesta.text.strip()
    if texto.startswith("```"):
        texto = texto.replace("```json", "").replace("```", "").strip()

    return json.loads(texto)


def ejecutar_agente():
    """
    Ciclo principal del agente:
    1. Conectar con la hoja vinculada a Google Forms.
    2. Leer respuestas.
    3. Analizar filas nuevas (columna F != 'SI').
    4. Guardar resultados en Sheets.
    """
    print("\n" + "=" * 60)
    print("🤖 Iniciando ciclo de revisión de respuestas...")
    print("=" * 60)

    hoja = conectar_sheets()
    if hoja is None:
        print("⚠️ No se pudo acceder a Google Sheets. Se reintentará en el próximo ciclo.")
        return

    try:
        todas_las_filas = hoja.get_all_values()
    except Exception as e:
        print(f"❌ Error al leer las filas: {e}")
        return

    # Omitir fila de encabezados
    filas_datos = todas_las_filas[1:]
    if not filas_datos:
        print("📭 Aún no hay respuestas registradas en el formulario.")
        return

    print(f"📋 Total de respuestas encontradas: {len(filas_datos)}")

    nuevas_procesadas = 0
    max_col_necesaria = max(COL_NOMBRE, COL_COMENTARIO, COL_CLASIFICACION, COL_SENTIMIENTO, COL_EJECUTADO)

    for indice, fila in enumerate(filas_datos, start=2):
        # Asegurar longitud mínima de la fila para evitar IndexError
        while len(fila) < max_col_necesaria:
            fila.append("")

        nombre_cliente = fila[COL_NOMBRE - 1]
        comentario     = fila[COL_COMENTARIO - 1]
        ya_procesada   = fila[COL_EJECUTADO - 1]

        # Saltar si ya fue procesada anteriormente
        if ya_procesada.strip().upper() == "SI":
            continue

        # Si el comentario está vacío, omitir
        if not comentario.strip():
            print(f"⚠️ Fila {indice}: Comentario vacío, omitiendo.")
            continue

        print(f"\n🔎 Procesando Fila {indice} | Cliente: {nombre_cliente or '(Anónimo / No especificado)'}")
        print(f"   💬 Comentario: {comentario[:80]}...")

        # Analizar con IA (con reintentos ante saturación o límites de cuota)
        resultado = None
        for intento in range(3):
            try:
                resultado = analizar_con_gemini(comentario)
                break
            except Exception as e:
                error_str = str(e)
                if "503" in error_str or "overloaded" in error_str.lower():
                    print(f"   ⏳ Servidor saturado (intento {intento + 1}/3). Esperando 10s...")
                    time.sleep(10)
                elif "429" in error_str or "quota" in error_str.lower():
                    print(f"   ⏳ Límite de cuota alcanzado (intento {intento + 1}/3). Esperando 20s...")
                    time.sleep(20)
                else:
                    print(f"   ❌ Error al analizar con Gemini: {e}")
                    break

        if not resultado:
            print(f"   ⚠️ No se pudo procesar la fila {indice}. Se intentará en la siguiente vuelta.")
            continue

        clasificacion = resultado.get("clasificación", "Desconocida")
        sentimiento   = resultado.get("sentimiento", "Desconocido")
        print(f"   🏷️ Clasificación: {clasificacion}")
        print(f"   ❤️ Sentimiento:   {sentimiento}")

        # Guardar en Google Sheets
        try:
            hoja.update_cell(indice, COL_CLASIFICACION, clasificacion)
            hoja.update_cell(indice, COL_SENTIMIENTO, sentimiento)
            hoja.update_cell(indice, COL_EJECUTADO, "SI")
            print(f"   💾 Fila {indice} actualizada exitosamente.")
            nuevas_procesadas += 1
        except Exception as e:
            print(f"   ❌ Error al escribir en Google Sheets: {e}")
            continue

        # Pausa para respetar la tasa de peticiones de la API
        time.sleep(PAUSA_ENTRE_LLAMADAS_SEG)

    print("\n" + "-" * 60)
    if nuevas_procesadas == 0:
        print("📭 No había comentarios nuevos por procesar.")
    else:
        print(f"✅ Ciclo finalizado. Total de respuestas procesadas: {nuevas_procesadas}")
    print("-" * 60)


if __name__ == "__main__":
    print("🚀 Iniciando Agente de Análisis de Google Forms...")

    # Validación de API Key
    if not GEMINI_API_KEY or GEMINI_API_KEY == "TU_API_KEY_AQUI":
        print("❌ Error: Debes configurar GEMINI_API_KEY.")
        print("   Obtén una clave en: AQ.Ab8RN6KUyqbZgds5qXE_i2RtopBSLhwFjdaJi2RZinM0M_ixrg")
        exit(1)

    # Validación de credenciales de Google
    if not os.path.exists(ARCHIVO_CREDENCIALES):
        print(f"❌ Error: Falta el archivo '{ARCHIVO_CREDENCIALES}'.")
        print("   Descarga tus credenciales OAuth de Google Cloud y colócalas aquí.")
        exit(1)

    # Inicializar cliente Gemini
    try:
        cliente_gemini = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ Cliente de Gemini inicializado correctamente.")
    except Exception as e:
        print(f"❌ Error al inicializar Gemini: {e}")
        exit(1)

    # Ejecutar primera revisión de inmediato
    print("▶️ Ejecutando primera revisión...")
    ejecutar_agente()

    # Programar revisiones automáticas periódicas
    schedule.every(INTERVALO_MINUTOS).minutes.do(ejecutar_agente)
    print(f"\n⏰ Agente programado para revisar cada {INTERVALO_MINUTOS} minutos.")
    print("   Presiona Ctrl+C en cualquier momento para detenerlo.\n")

    while True:
        try:
            schedule.run_pending()
            time.sleep(15)
        except KeyboardInterrupt:
            print("\n🛑 Agente detenido por el usuario. ¡Hasta pronto!")
            break
        except Exception as e:
            print(f"⚠️ Error en el bucle principal: {e}")
            time.sleep(15)
