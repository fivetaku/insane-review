[English](README.md) | [한국어](README.ko.md) | [中文](README.zh.md) | [日本語](README.ja.md) | Español

# insane-review

<div align="center">
  <img src="assets/hero.png" width="860" alt="héroe cinematográfico de insane-review">
</div>

> **GPT Pro (el nivel de razonamiento Pro del buque insignia actual — actualmente GPT-5.6 Sol) no tiene API. Este plugin lo usa igualmente desde dentro de Claude Code.**

GPT Pro solo vive en la aplicación web de ChatGPT (suscripción) — no existe una API oficial, así que no puedes alcanzarlo desde Codex CLI, `omc ask` ni un proveedor de API de agent-council. insane-review controla **tu sesión web de ChatGPT ya iniciada a través de CDP**: empaqueta el código relevante con repomix, se lo envía a Pro y recoge la revisión. **Coste de API cero** — funciona sobre tu plan de ChatGPT existente.

[Inicio rápido](#inicio-rápido) • [¿Por qué insane-review?](#por-qué-insane-review) • [Cómo funciona](#cómo-funciona) • [Funciones](#funciones) • [Ajustes y tiempos de espera](#ajustes-y-tiempos-de-espera) • [Requisitos](#requisitos)

---

## Inicio rápido

### 1. Añade el marketplace (una sola vez)

```
/plugin marketplace add https://github.com/fivetaku/gptaku_plugins.git
```

### 2. Instala

```
/plugin install insane-review
```

### 3. Reinicia Claude Code

Necesario para que el plugin se cargue.

### 4. Prepara el puente del navegador (una vez por máquina)

Pro es solo web, así que insane-review necesita un navegador real, con sesión iniciada, en un puerto de depuración:

```bash
# launch Comet (or Chrome) with the CDP port, then log into chatgpt.com and pick the Pro reasoning tier
open -a Comet --args --remote-debugging-port=9222

# verify everything is wired up (node/repomix, playwright, pyperclip, CDP browser)
python3 bin/pack_and_ask.py --check-env
```

### 5. Ejecútalo

```
/insane-review review the auth flow in src/auth
```

O simplemente di "que Pro revise esto" / "pregunta a GPT Pro sobre este diseño" — Claude identifica el objetivo y lo empaqueta.

---

## ¿Por qué insane-review?

- **La única forma de llegar a Pro de manera programática** — no existe API. Una sesión web con inicio de sesión, controlada por CDP, es el puente, y no cuesta nada más allá de tu suscripción.
- **Claude elige el conjunto relevante completo** — no tienes que enumerar archivos a mano. Para las revisiones envía el **código completo** (sin `--compress`, que elimina los cuerpos de las funciones y produce falsos veredictos de "parece bien") y audita la lista de archivos empaquetados para que nada falte en silencio.
- **fail-closed por diseño** — un modelo equivocado, un inicio de sesión sin verificar, un prompt truncado, un paquete vacío o la respuesta de un turno anterior se rechazan en lugar de enviarse o guardarse en silencio. Endurecido a lo largo de cuatro rondas de autorevisión del propio Pro (6 → 0 P0).
- **Dos roles, un solo motor** — un revisor independiente cuando pides una corrección/revisión, o un miembro solo-web de [agent-council](references/council-setup.md) para que Pro debata junto a Codex/Gemini y otros.
- **Citas que puedes seguir** — los números de línea van dentro del paquete, así que los hallazgos de Pro vuelven como `file:line` a los que puedes saltar directamente.

---

## Cómo funciona

```
"have Pro review this"  /  council member call
  ↓
Claude selects the COMPLETE relevant file set (full code — no --compress for reviews)
  ↓
repomix pack  (line numbers · secretlint · packed-file-list audit · token count)
  ↓
CDP-attach the logged-in ChatGPT session
Select Pro effort (flagship auto-follows; currently GPT-5.6 Sol)  → re-open menu and VERIFY (mismatch = abort, fail-closed)
  ↓
Attach pack + prompt  → confirm the prompt actually landed in the composer  → send
  ↓
Wait for THIS turn to complete (turn-scoped: new assistant node + new copy button)
Optionally cut long reasoning early with --force-answer-after
  ↓
Harvest the answer → save to  ./.insane-review/response_*.md  (atomic write)
```

La salida se guarda en la carpeta `.insane-review/` del **proyecto actual** (como `.kkirikkiri/` de kkirikkiri), nunca dentro del plugin:

```
.insane-review/
├── pack_<target>_<ts>.md        # what was sent (chmod 600)
└── response_<target>_<ts>.md    # Pro's answer + verified model header
```

---

## Funciones

### Comandos

| Comando | Qué hace |
|---------|-------------|
| `/insane-review [target/question]` | Empaqueta el código relevante y se lo envía a GPT Pro para revisión |
| lenguaje natural | "que Pro revise esto", "pregunta a GPT Pro sobre X" — el mismo flujo |

### Dos modos

1. **Revisor independiente** — pides una corrección/revisión → Claude delimita el objetivo → paquete repomix → análisis de Pro → aplicado de vuelta.
2. **Miembro de agent-council** — registra a Pro como miembro solo-web del council para que debata con otros modelos. Ver [`references/council-setup.md`](references/council-setup.md).

### Flags principales

| Flag | Propósito |
|------|---------|
| `--target <dir>` | Carpeta a empaquetar (omítelo para una opinión solo con prompt) |
| `--include <glob>` / `--ignore <glob>` | Acota el conjunto empaquetado |
| `--effort <nivel>` (alias `--model`) | Esfuerzo de razonamiento — por defecto `pro`; `instant`/`medium`/`high`/`xhigh` se aplican tal cual y una discrepancia aborta |
| `--require-model "<nombre>"` | Fija el nombre del modelo — aborta si no coincide. **La UI actual solo muestra el nombre del modelo en el nivel Pro (p. ej. `6`); en otros niveles no se puede leer, así que siempre aborta.** Omítelo para seguir al modelo insignia |
| `--prompt "..."` / `--prompt-file` | La pregunta |
| `--pack-only` | Solo empaqueta (inspecciona el recuento de tokens), sin enviar |
| `--council` | Modo council — respuesta por stdout, logs por stderr |
| `--compress` | Solo esqueleto tree-sitter — **no lo uses para revisiones** (elimina los cuerpos de las funciones) |
| `--check-env` / `--install` | Diagnostica / instala las herramientas locales |

---

## Ajustes y tiempos de espera

La espera de respuesta y los tiempos de empaquetado se pueden ajustar tanto desde la CLI como desde el entorno — útil porque el razonamiento completo de Pro puede tardar 10–15 minutos.

| Control | Por defecto | Qué hace |
|---------|---------|-------------|
| `--max-wait <sec>` | `1200` (20 min) | Tiempo máximo de espera por la respuesta de Pro antes de rendirse (fail-closed, sin guardado parcial) |
| `INSANE_REVIEW_MAX_WAIT` | `1200` | Igual que `--max-wait`, vía entorno |
| `--force-answer-after <sec>` | off | Corte suave: si Pro sigue razonando tras N segundos, pulsa **"Get answer now"** para que responda **con el razonamiento hecho hasta ese momento** — una respuesta completa y guardada (ver abajo) |
| `INSANE_REVIEW_REPOMIX_TIMEOUT` | `300` | Segundos máximos para el paso de empaquetado de repomix |
| `--retries <n>` | `1` | Reintentos si falla un envío/recogida |

**Dos "timeouts" distintos — no los confundas:**

- **`--force-answer-after N` (corte suave, recomendado para acotar el coste).** Pro razona durante mucho tiempo; esta opción pulsa el *"Get answer now"* de ChatGPT a los N segundos, de modo que Pro deja de razonar y responde basándose en **lo que ha razonado hasta ese punto**. Esa respuesta es un turno normal y completo — se recoge y se guarda como cualquier otra. Úsalo para limitar a un miembro del council a, por ejemplo, 120 s en lugar de esperar más de 10 minutos.
- **`--max-wait N` (techo duro, fail-closed).** Si el turno nunca se completa en N segundos *y* no se forzó ninguna respuesta, insane-review se rinde **sin guardar** el texto a medio transmitir — una respuesta incompleta se trata como un fallo, no como un resultado. Es intencionado: nunca te entrega una revisión truncada que finge estar terminada.

Otras variables de entorno:

| Variable | Por defecto | Qué hace |
|----------|---------|-------------|
| `INSANE_REVIEW_CDP_PORT` | `9222` | Puerto de depuración remota del navegador |
| `INSANE_REVIEW_COMET` / `INSANE_REVIEW_CHROME` | ruta por defecto de la app | Ruta del ejecutable del navegador |
| `INSANE_REVIEW_REPOMIX_VERSION` | `1.15.0` | Versión fijada de repomix (reproducibilidad) |
| `INSANE_REVIEW_OUT` | `./.insane-review` | Directorio de salida (también `--out-dir`) |

```bash
# example: give Pro up to 25 minutes, but cut reasoning at 5 minutes if it's still thinking
INSANE_REVIEW_MAX_WAIT=1500 python3 bin/pack_and_ask.py \
  --target . --include "src/**" --model pro \
  --force-answer-after 300 --prompt "Where are the concurrency bugs?"
```

---

## Requisitos

### Obligatorio

- [Claude Code](https://docs.anthropic.com/claude-code)
- Python 3.11+ con `playwright` y `pyperclip`
- Node.js / `npx`
- **Una cuenta de ChatGPT por suscripción con GPT Pro**, con sesión iniciada en un Comet/Chrome lanzado con el puerto de depuración (`--remote-debugging-port=9222`)

### Qué se gestiona solo vs. qué haces tú

| Dependencia | Comportamiento en el primer uso |
|------------|-------------------|
| **repomix** | **Totalmente automático** — se descarga bajo demanda vía `npx -y repomix@<pinned>`, nunca requiere instalación manual |
| **playwright / pyperclip** | Se comprueban en el primer uso con `--check-env`; instálalos con `--install` (ejecuta `pip install`). Una ejecución normal sin ellos se detiene con una instrucción clara (fail-closed) en lugar de fallar a mitad de camino |
| **Inicio de sesión del navegador + GPT Pro** | **Manual** — no se puede automatizar; inicias sesión en `chatgpt.com` y seleccionas Pro una vez |

```bash
# one shot: checks node/repomix, playwright, pyperclip, CDP browser — and installs the pip deps if missing
python3 bin/pack_and_ask.py --check-env --install
```

### Nota

La automatización de la interfaz web no está avalada por los ToS de OpenAI, y los selectores pueden requerir mantenimiento cuando el DOM de ChatGPT cambie. Pensado únicamente para uso personal con suscripción.

---

## Licencia

MIT

---

<div align="center">

**No API. Still Pro.**

</div>
