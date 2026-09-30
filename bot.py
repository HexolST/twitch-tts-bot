import os
import json
import time
import asyncio
import uuid
import re
import random
import websockets
from num2words import num2words
from dotenv import load_dotenv
import requests
import pygame
import twitchio
from twitchio.ext import commands

load_dotenv()

TWITCH_TOKEN = os.getenv("TWITCH_TOKEN")
TWITCH_CHANNEL = os.getenv("TWITCH_CHANNEL")
FISH_API_KEY = os.getenv("FISH_API_KEY")

pygame.mixer.init()

CONFIG_PATH = "config.json"
VOCES_USUARIOS_PATH = "voces_usuarios.json"
STREAMERBOT_WS_URL = "ws://127.0.0.1:8080/"

PATRON_CARACTERES_PERMITIDOS = re.compile(r"[^a-zA-Z0-9áéíóúÁÉÍÓÚñÑüÜ¿?¡!.,:;\-'\"() \n]")


def cargar_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def cargar_voces_usuarios():
    try:
        with open(VOCES_USUARIOS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def guardar_voces_usuarios(datos):
    with open(VOCES_USUARIOS_PATH, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def obtener_voz_usuario(usuario):
    usuario = usuario.lower()
    voces_usuarios = cargar_voces_usuarios()
    config = cargar_config()
    voces_disponibles = config.get("voices", {})

    voz_actual = voces_usuarios.get(usuario)

    if voz_actual and voz_actual in voces_disponibles.values():
        return voz_actual

    opciones = [vid for nombre, vid in voces_disponibles.items() if nombre != "default"]
    if not opciones:
        opciones = list(voces_disponibles.values())
    if not opciones:
        return ""

    nueva_voz = random.choice(opciones)
    voces_usuarios[usuario] = nueva_voz
    guardar_voces_usuarios(voces_usuarios)
    return nueva_voz


def limpiar_texto_raro(texto):
    return PATRON_CARACTERES_PERMITIDOS.sub("", texto)


def normalizar_numeros(texto):
    def reemplazar(match):
        numero = int(match.group())
        return num2words(numero, lang="es")
    return re.sub(r"\d+", reemplazar, texto)


def generar_audio(texto, nombre_archivo="salida.mp3", voice_id_override=None):
    config = cargar_config()

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_API_KEY}",
        "Content-Type": "application/json",
        "model": "s2.1-pro-free",
    }

    texto = limpiar_texto_raro(texto).strip()

    if len(texto) == 0:
        print("⚠️ El texto quedó vacío después de filtrar símbolos, se omite el audio")
        return False

    texto = normalizar_numeros(texto)
    data = {"text": texto, "format": "mp3"}

    voice_id = voice_id_override if voice_id_override else config.get("voice_id", "")
    if voice_id:
        data["reference_id"] = voice_id

    response = requests.post(url, headers=headers, json=data)

    if response.status_code == 200:
        with open(nombre_archivo, "wb") as f:
            f.write(response.content)
        return True
    else:
        print(f"❌ Error {response.status_code}: {response.text}")
        return False


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL]
        )
        self.cola = asyncio.Queue()
        self.ultima_vez = {}
        self.ultimo_canje = None

    async def event_ready(self):
        print(f"✅ Conectado como bot al canal: {TWITCH_CHANNEL}")
        asyncio.create_task(self.procesar_cola())

        if cargar_config().get("streamerbot_enabled", False):
            asyncio.create_task(self.conectar_streamerbot())
        else:
            print("ℹ️ Streamer.bot desactivado (streamerbot_enabled: false) — el bot funciona igual solo por chat.")

    async def procesar_cola(self):
        while True:
            usuario, texto = await self.cola.get()
            print(f"🎙️ Generando audio para: {texto} (usuario: {usuario})")

            nombre_archivo = f"audio_{uuid.uuid4().hex}.mp3"

            config = cargar_config()
            voz_override = None
            if config.get("modo_personajes", False) and usuario:
                voz_override = obtener_voz_usuario(usuario)

            if generar_audio(texto, nombre_archivo, voz_override):
                pygame.mixer.music.load(nombre_archivo)
                pygame.mixer.music.play()

                tiempo_inicio = time.time()
                duracion_maxima = (len(texto) * 0.1) + 5

                while pygame.mixer.music.get_busy():
                    if time.time() - tiempo_inicio > duracion_maxima:
                        pygame.mixer.music.stop()
                        print(f"⚠️ Audio cortado por exceder la duración máxima ({duracion_maxima:.1f}s)")
                        break
                    await asyncio.sleep(0.1)

                pygame.mixer.music.unload()

                try:
                    os.remove(nombre_archivo)
                except OSError:
                    pass

            self.cola.task_done()

    async def conectar_streamerbot(self):
        while True:
            try:
                async with websockets.connect(STREAMERBOT_WS_URL) as ws:
                    print("✅ Conectado a Streamer.bot")

                    suscripcion = {
                        "request": "Subscribe",
                        "id": "tts-sub-1",
                        "events": {"Twitch": ["RewardRedemption"]}
                    }
                    await ws.send(json.dumps(suscripcion))

                    async for mensaje in ws:
                        evento = json.loads(mensaje)
                        tipo = evento.get("event", {}).get("type")
                        if tipo != "RewardRedemption":
                            continue

                        data = evento.get("data", {})
                        reward_id_recibido = data.get("reward", {}).get("id", "")
                        config = cargar_config()

                        self.ultimo_canje = {
                            "id": reward_id_recibido,
                            "titulo": data.get("reward", {}).get("title", "")
                        }

                        reward_id_voz = config.get("reward_id_voz", "")
                        if reward_id_voz and reward_id_recibido == reward_id_voz:
                            texto_input = data.get("user_input", "").strip().lower()
                            voces = config.get("voices", {})

                            if texto_input in voces:
                                nueva_voz = voces[texto_input]
                                nombre_elegido = texto_input
                            else:
                                opciones = [n for n in voces.keys() if n != "default"]
                                if not opciones:
                                    opciones = list(voces.keys())
                                if not opciones:
                                    continue

                                nombre_elegido = random.choice(opciones)
                                nueva_voz = voces[nombre_elegido]

                            if config.get("modo_personajes", False):
                                usuario_canje_voz = data.get("user_login", "").strip().lower()
                                voces_usuarios = cargar_voces_usuarios()
                                voces_usuarios[usuario_canje_voz] = nueva_voz
                                guardar_voces_usuarios(voces_usuarios)
                                print(f"🔀 Voz de {usuario_canje_voz} cambiada a: {nombre_elegido}")
                            else:
                                config["voice_id"] = nueva_voz
                                guardar_config(config)
                                print(f"🔀 Voz cambiada a: {nombre_elegido}")
                            continue

                        reward_id_configurado = config.get("reward_id", "")
                        if reward_id_configurado and reward_id_recibido != reward_id_configurado:
                            continue

                        texto = data.get("user_input", "")
                        usuario_canje = data.get("user_login", "").strip().lower()
                        if texto.strip():
                            await self.cola.put((usuario_canje, texto.strip()))

            except Exception as e:
                print(f"⚠️ Conexión con Streamer.bot perdida o falló: {e}")
                print("Reintentando en 5 segundos...")
                await asyncio.sleep(5)

    def es_mod_o_broadcaster(self, author):
        es_broadcaster = author.name.lower() == TWITCH_CHANNEL.lower()
        es_mod = getattr(author, "is_mod", False)
        return es_mod or es_broadcaster

    async def event_message(self, message):
        if message.echo:
            return

        print(f"[{message.author.name}]: {message.content}")

        contenido = message.content.strip()
        partes = contenido.split(" ", 1)
        palabra_clave = partes[0].lower()

        config = cargar_config()
        es_mod = self.es_mod_o_broadcaster(message.author)

        if palabra_clave in ("!ttson", "!ttsoff"):
            if not es_mod:
                return
            config["tts_enabled"] = (palabra_clave == "!ttson")
            guardar_config(config)
            estado = "activado ✅" if config["tts_enabled"] else "desactivado ❌"
            await message.channel.send(f"TTS {estado}")
            return

        if palabra_clave == "!ttssubs":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !ttssubs on / !ttssubs off")
                return
            valor = partes[1].strip().lower()
            if valor in ("on", "activado"):
                config["subs_only"] = True
            elif valor in ("off", "desactivado"):
                config["subs_only"] = False
            else:
                await message.channel.send("Uso: !ttssubs on / !ttssubs off")
                return
            guardar_config(config)
            estado = "activado" if config["subs_only"] else "desactivado"
            await message.channel.send(f"Modo solo-subs {estado}")
            return

        if palabra_clave == "!ttsvips":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !ttsvips on / !ttsvips off")
                return
            valor = partes[1].strip().lower()
            if valor in ("on", "activado"):
                config["vips_only"] = True
            elif valor in ("off", "desactivado"):
                config["vips_only"] = False
            else:
                await message.channel.send("Uso: !ttsvips on / !ttsvips off")
                return
            guardar_config(config)
            estado = "activado" if config["vips_only"] else "desactivado"
            await message.channel.send(f"Modo solo-VIPs {estado}")
            return

        if palabra_clave == "!ttsartists":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !ttsartists on / !ttsartists off")
                return
            valor = partes[1].strip().lower()
            if valor in ("on", "activado"):
                config["artists_only"] = True
            elif valor in ("off", "desactivado"):
                config["artists_only"] = False
            else:
                await message.channel.send("Uso: !ttsartists on / !ttsartists off")
                return
            guardar_config(config)
            estado = "activado" if config["artists_only"] else "desactivado"
            await message.channel.send(f"Modo solo-artistas {estado}")
            return

        if palabra_clave == "!modopersonajes":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !modopersonajes on / !modopersonajes off")
                return
            valor = partes[1].strip().lower()
            if valor in ("on", "activado"):
                config["modo_personajes"] = True
            elif valor in ("off", "desactivado"):
                config["modo_personajes"] = False
            else:
                await message.channel.send("Uso: !modopersonajes on / !modopersonajes off")
                return
            guardar_config(config)
            estado = "activado 🎭" if config["modo_personajes"] else "desactivado"
            await message.channel.send(f"Modo personajes {estado}")
            return

        if palabra_clave == "!addvoice":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !addvoice nombre reference_id")
                return
            argumentos = partes[1].split(" ", 1)
            if len(argumentos) < 2:
                await message.channel.send("Uso: !addvoice nombre reference_id")
                return
            nombre_voz = argumentos[0].strip().lower()
            id_voz = argumentos[1].strip()
            voces = config.get("voices", {})
            voces[nombre_voz] = id_voz
            config["voices"] = voces
            guardar_config(config)
            await message.channel.send(f"Voz '{nombre_voz}' agregada ✅")
            return

        if palabra_clave == "!removevoice":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !removevoice nombre")
                return
            nombre_voz = partes[1].strip().lower()
            voces = config.get("voices", {})

            if nombre_voz == "default":
                await message.channel.send("No se puede eliminar la voz 'default'")
                return

            if nombre_voz in voces:
                del voces[nombre_voz]
                config["voices"] = voces
                guardar_config(config)
                await message.channel.send(f"Voz '{nombre_voz}' eliminada ✅")
            else:
                await message.channel.send(f"No existe una voz llamada '{nombre_voz}'")
            return

        if palabra_clave == "!setvoice":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !setvoice nombre")
                return
            nombre_voz = partes[1].strip().lower()
            voces = config.get("voices", {})

            if nombre_voz in voces:
                config["voice_id"] = voces[nombre_voz]
                guardar_config(config)
                await message.channel.send(f"Voz cambiada a '{nombre_voz}' ✅")
            else:
                disponibles = ", ".join(voces.keys())
                await message.channel.send(f"No existe la voz '{nombre_voz}'. Disponibles: {disponibles}")
            return

        if palabra_clave == "!setuservoice":
            if not es_mod:
                return
            if len(partes) < 2:
                await message.channel.send("Uso: !setuservoice usuario nombre_voz")
                return
            argumentos = partes[1].split(" ", 1)
            if len(argumentos) < 2:
                await message.channel.send("Uso: !setuservoice usuario nombre_voz")
                return
            usuario_objetivo = argumentos[0].strip().lower().lstrip("@")
            nombre_voz = argumentos[1].strip().lower()
            voces = config.get("voices", {})

            if nombre_voz not in voces:
                disponibles = ", ".join(voces.keys())
                await message.channel.send(f"No existe la voz '{nombre_voz}'. Disponibles: {disponibles}")
                return

            voces_usuarios = cargar_voces_usuarios()
            voces_usuarios[usuario_objetivo] = voces[nombre_voz]
            guardar_voces_usuarios(voces_usuarios)
            await message.channel.send(f"Voz de @{usuario_objetivo} cambiada a '{nombre_voz}' ✅")
            return

        if palabra_clave == "!voicelist":
            voces = config.get("voices", {})
            if not voces:
                await message.channel.send("No hay voces configuradas todavía.")
                return
            lista = ", ".join(voces.keys())
            await message.channel.send(f"Voces disponibles: {lista}")
            return

        if palabra_clave == "!ultimocanje":
            if not es_mod:
                return
            if not self.ultimo_canje:
                await message.channel.send("Todavía no llegó ningún canje.")
                return
            await message.channel.send(
                f"Último canje: '{self.ultimo_canje['titulo']}' → id: {self.ultimo_canje['id']}"
            )
            return

        if palabra_clave == "!ttscomandos":
            comandos_publicos = "!voicelist, !s <mensaje>"
            comandos_mod = "!ttson, !ttsoff, !ttssubs on/off, !ttsvips on/off, !ttsartists on/off, !modopersonajes on/off, !setvoice <nombre>, !addvoice <nombre> <id>, !removevoice <nombre>, !setuservoice <usuario> <voz>, !ultimocanje"

            if es_mod:
                await message.channel.send(f"Comandos: {comandos_publicos} | Mods: {comandos_mod}")
            else:
                await message.channel.send(f"Comandos: {comandos_publicos}")
            return

        comando = config.get("command", "!s")
        if not contenido.startswith(comando + " "):
            return

        if not config.get("tts_enabled", True):
            return

        usuario = message.author.name.lower()
        allowed = [u.lower() for u in config.get("allowed_users", [])]
        blocked = [u.lower() for u in config.get("blocked_users", [])]

        if usuario in blocked:
            return

        restricciones_activas = []
        if config.get("subs_only", False):
            restricciones_activas.append(message.author.is_subscriber)
        if config.get("vips_only", False):
            restricciones_activas.append(getattr(message.author, "is_vip", False))
        if config.get("artists_only", False):
            badges = getattr(message.author, "badges", {}) or {}
            restricciones_activas.append("artist-badge" in badges)

        cumple_alguna = any(restricciones_activas) if restricciones_activas else True

        if restricciones_activas and not cumple_alguna and usuario not in allowed and not es_mod:
            return

        cooldown = config.get("cooldown_seconds", 5)
        ahora = time.time()
        ultima = self.ultima_vez.get(usuario, 0)
        if ahora - ultima < cooldown:
            return
        self.ultima_vez[usuario] = ahora

        texto = contenido[len(comando) + 1:].strip()
        if len(texto) == 0:
            return

        max_len = config.get("max_message_length", 200)
        if len(texto) > max_len:
            texto = texto[:max_len]

        await self.cola.put((usuario, texto))


if __name__ == "__main__":
    import traceback

    print("Iniciando bot...")
    try:
        bot = Bot()
        bot.run()
    except Exception:
        print("❌ Ocurrió un error al ejecutar el bot:")
        traceback.print_exc()
