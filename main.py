import os, re, json, random, time, base64, glob, unicodedata, string
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import cv2, numpy as np
from PIL import Image
import io
import anthropic
import requests   # para consultar o Supabase

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("⚠️ playwright-stealth não instalado. Execute: pip install playwright-stealth")

load_dotenv()

CIWEB_URL         = os.getenv("CIWEB_URL", "https://www.ciweb.caixa.gov.br/sso/")
CIWEB_USER        = os.getenv("CIWEB_USER", "")
CIWEB_PASS        = os.getenv("CIWEB_PASS", "")
CARTEIRA          = os.getenv("CARTEIRA", "CAIXA - FGTS Moradia Própria")
PROFILE_DIR       = "profile_ciweb"
MEMORY_FILE       = "captcha_memory.json"
OBJECT_CACHE_FILE = "object_cache.json"

ANTHROPIC_MODEL    = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
ANTHROPIC_FALLBACK = os.getenv("ANTHROPIC_FALLBACK", "claude-haiku-4-5-20251001")
VALID_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5-20250929",
]

CODIGO_ESPERA_INICIAL_SEGUNDOS = int(os.getenv("CODIGO_ESPERA_INICIAL_SEGUNDOS", "60"))
CODIGO_TENTATIVAS_SUPABASE = int(os.getenv("CODIGO_TENTATIVAS_SUPABASE", "6"))
CODIGO_INTERVALO_CONSULTA_SEGUNDOS = int(os.getenv("CODIGO_INTERVALO_CONSULTA_SEGUNDOS", "5"))
CODIGO_TIMEOUT_DETECCAO_MS = int(os.getenv("CODIGO_TIMEOUT_DETECCAO_MS", "15000"))

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = s.lower().strip()
    s = re.sub(r"[:\n\r\t]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def _random_digits(n: int, first_non_zero: bool = True) -> str:
    if n <= 0:
        return ""
    if first_non_zero:
        return str(random.randint(1, 9)) + "".join(random.choice(string.digits) for _ in range(n - 1))
    return "".join(random.choice(string.digits) for _ in range(n))

def gerar_dados_ficticios_imovel() -> dict:
    ceps_validos = [
        "01001000",  # Praça da Sé - SP
        "01310930",  # Av Paulista - SP
        "20040002",  # Centro - RJ
        "30140071",  # Funcionários - BH
        "40010000",  # Centro - Salvador
        "70040900",  # Brasília
        "80010000",  # Centro - Curitiba
        "90010000",  # Centro - Porto Alegre
    ]

    bloco_base = random.choice(list("ABCDEFGHJKLMNPQRSTUVWXYZ"))
    bloco = bloco_base if random.random() < 0.6 else f"{bloco_base}{random.randint(1,9)}"

    return {
        "matricula": _random_digits(random.randint(7, 10), first_non_zero=True),
        "cep": random.choice(ceps_validos),
        "numero": str(random.randint(10, 9999)),
        "numero_complemento": str(random.randint(1, 999)),
        "bloco": bloco,
        "marcar_empreendimento": True,
    }

_anthropic = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

_STRIP = (
    "selecione todas as imagens com ", "selecione todos os quadrados com ",
    "clique em todos os quadrados com ", "clique em todas as imagens com ",
    "uma ", "um ", "umas ", "uns ", "as ", "os ", "a ", "o ",
)

class DynamicObjectClassifier:
    def __init__(self, path: str = OBJECT_CACHE_FILE):
        self.path = path
        self._cache: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                erradas = [k for k, e in self._cache.items() if "erro LLM" in e.get("reason", "")]
                if erradas:
                    for k in erradas:
                        del self._cache[k]
                    self._save()
                    log(f"⚠️ Cache: {len(erradas)} entrada(s) com erro removida(s).")
                log(f"Cache objetos: {len(self._cache)} entradas")
            except Exception as e:
                log(f"⚠️ Erro cache objetos: {e}")
        else:
            log("Cache de objetos novo.")

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log(f"⚠️ Erro salvar cache: {e}")

    def _normalize_key(self, question: str) -> str:
        key = question.lower().strip()
        for prefix in _STRIP:
            if key.startswith(prefix):
                key = key[len(prefix):]
                break
        return key

    def classify(self, question: str):
        key = self._normalize_key(question)
        if key in self._cache:
            log(f"Cache hit '{key}' → Vision")
            return (None, True, key)
        log(f"Consultando LLM para '{key}'...")
        result = self._ask_llm(key, question)
        self._cache[key] = result
        self._save()
        return (None, True, key)

    def _ask_llm(self, key, original_question) -> dict:
        prompt = f'''Objeto do captcha: "{key}"
Pergunta original: "{original_question}"
Responda APENAS JSON: {{"yolo_class": null, "detectable": true, "reason": "usando Vision"}}'''
        try:
            resp = _anthropic_call_with_retry(
                messages=[{"role": "user", "content": prompt}], max_tokens=150)
            if not resp:
                return {"yolo_class": None, "detectable": True, "reason": "erro API Anthropic"}
            raw = _extract_text(resp)
            data = _parse_json_safe(raw)
            return {
                "yolo_class": None,
                "detectable": True,
                "reason": data.get("reason", "Vision") if data else "Vision"
            }
        except Exception as e:
            log(f"⚠️ LLM classify falhou: {e}")
            return {"yolo_class": None, "detectable": True, "reason": f"Vision: {e}"}

class VisionSolver:
    def analyze_grid(self, grid_cv: np.ndarray, object_name: str, grid: int) -> list[int]:
        enhanced = enhance(grid_cv)
        success, buf = cv2.imencode(".png", enhanced)
        if not success:
            return []
        img_b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")
        if grid == 3:
            grid_diagram = "0 | 1 | 2\n3 | 4 | 5\n6 | 7 | 8"
        elif grid == 4:
            grid_diagram = "0  | 1  | 2  | 3\n4  | 5  | 6  | 7\n8  | 9  | 10 | 11\n12 | 13 | 14 | 15"
        else:
            grid_diagram = f"Grade {grid}×{grid} (0 a {grid*grid-1})"
        prompt = f'''Você é um especialista em resolver captchas reCAPTCHA.

Esta imagem é um captcha dividido em grade {grid}×{grid}. Tiles numerados (0-based, esquerda→direita, cima→baixo):
{grid_diagram}

TAREFA: encontre todos os tiles que contêm "{object_name}".

CRITÉRIO:
✅ SIM → contém qualquer parte identificável do objeto: roda, quadro, guidão, carenagem, estrutura, etc.
✅ SIM → objeto parcialmente cortado na borda do tile
✅ SIM → imagem com ruído mas silhueta/forma reconhecível
❌ NÃO → apenas fundo, rua, muro, céu, vegetação, poste, calçada SEM nenhuma parte do objeto

Responda SOMENTE com JSON válido:
{{"tiles": [índices], "reason": "descreva brevemente o que viu em cada tile marcado"}}'''
        try:
            resp = _anthropic_call_with_retry([{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt},
            ]}], max_tokens=600)
            if not resp:
                return []
            raw = _extract_text(resp)
            log(f"  🧠 Raw: {raw[:250]!r}")
            data = _parse_json_safe(raw)
            if not data:
                return []
            tiles = [int(i) for i in data.get("tiles", []) if 0 <= int(i) < grid * grid]
            log(f"  🧠 Vision: tiles={tiles} | {data.get('reason','')}")
            return tiles
        except Exception as e:
            log(f"⚠️ Vision grid falhou: {e}")
            return []

    def analyze_single_tile(self, tile_cv: np.ndarray, object_name: str) -> bool:
        enhanced_tile = enhance_tile(tile_cv)
        success, buf = cv2.imencode(".png", enhanced_tile)
        if not success:
            return False
        img_b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")
        prompt = f'''Tile de captcha reCAPTCHA. Objeto procurado: "{object_name}"
- true se o objeto aparece, mesmo que PARCIAL. Confidence ≥ 0.35 já conta.
Responda APENAS com JSON: {{"has_object": true_ou_false, "confidence": 0.0_a_1.0, "reason": "breve"}}'''
        try:
            resp = _anthropic_call_with_retry([{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": prompt},
            ]}], max_tokens=150)
            if resp:
                data = _parse_json_safe(_extract_text(resp))
                if data:
                    has = bool(data.get("has_object", False))
                    conf = float(data.get("confidence", 0.0))
                    return has and conf >= 0.35
        except Exception as e:
            log(f"⚠️ Vision tile falhou: {e}")
        return False

def _extract_text(resp) -> str:
    if not resp or not resp.content:
        return ""
    full = ""
    for block in resp.content:
        if hasattr(block, "text") and block.text:
            full += block.text
    return full.strip()

def _parse_json_safe(raw: str) -> dict:
    if not raw:
        return {}
    clean = re.sub(r"```json|```", "", raw).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass
    match = re.search(r'\{[^{}]*\}', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    tiles_match = re.search(r'"tiles"\s*:\s*\[([^\]]*)\]', clean)
    if tiles_match:
        nums = re.findall(r'\d+', tiles_match.group(1))
        return {"tiles": [int(n) for n in nums], "reason": "extraído por regex"}
    return {}

def _anthropic_call_with_retry(messages, max_tokens: int, model: str = None, max_retries: int = 3):
    model = model or ANTHROPIC_MODEL
    for attempt in range(max_retries):
        try:
            resp = _anthropic.messages.create(model=model, max_tokens=max_tokens, messages=messages)
            text = _extract_text(resp)
            if not text and getattr(resp, "stop_reason", "") == "max_tokens":
                resp = _anthropic.messages.create(model=model, max_tokens=max_tokens * 2, messages=messages)
            return resp
        except Exception as e:
            error_msg = str(e)
            log(f"⚠️ Anthropic tentativa {attempt+1}/{max_retries} com '{model}' falhou: {error_msg}")
            if "404" in error_msg or "not_found_error" in error_msg:
                for next_model in VALID_MODELS:
                    if next_model == model:
                        continue
                    try:
                        return _anthropic.messages.create(model=next_model, max_tokens=max_tokens, messages=messages)
                    except Exception:
                        continue
                return None
            if attempt < max_retries - 1:
                wait = 1.5 * (attempt + 1)
                log(f"⏳ Aguardando {wait:.1f}s...")
                time.sleep(wait)
    return None

dynamic_classifier = DynamicObjectClassifier()
vision_solver = VisionSolver()

MAX_DIST = 10

def phash(tile_cv: np.ndarray) -> str:
    gray = cv2.cvtColor(cv2.resize(tile_cv, (32, 32)), cv2.COLOR_BGR2GRAY).astype(np.float32)
    dct = cv2.dct(gray)[:8, :8].flatten()
    median = np.median(dct)
    return f"{int(''.join('1' if b > median else '0' for b in dct), 2):016x}"

def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")

class TileMemory:
    def __init__(self, path: str = MEMORY_FILE):
        self.path = path
        self._data: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                total = len(self._data)
                pos = sum(1 for e in self._data.values() if e["is_target"])
                log(f"Memória: {total} tiles ({pos}✅ / {total-pos}❌)")
            except Exception as e:
                log(f"⚠️ Erro memória: {e}")
        else:
            log("Memória nova.")

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log(f"⚠️ Erro salvar memória: {e}")

    def lookup(self, tile_cv: np.ndarray, cls: str):
        h = phash(tile_cv)
        for sh, e in self._data.items():
            if e.get("class") == cls and hamming(h, sh) <= MAX_DIST:
                return bool(e["is_target"])
        return None

    def record(self, tile_cv: np.ndarray, cls: str, is_target: bool):
        h = phash(tile_cv)
        for sh, e in self._data.items():
            if e.get("class") == cls and hamming(h, sh) <= MAX_DIST:
                e["hits"] += 1
                e["last_seen"] = _now()
                self._save()
                return
        self._data[h] = {"class": cls, "is_target": is_target, "hits": 1, "last_seen": _now()}
        self._save()

    def commit(self, tiles: dict, clicked: set, cls: str):
        pos = neg = 0
        for idx, t in tiles.items():
            is_t = idx in clicked
            self.record(t, cls, is_t)
            if is_t:
                pos += 1
            else:
                neg += 1
        log(f"Memória atualizada: +{pos}✅ +{neg}❌ total={len(self._data)}")

memory = TileMemory()

def enhance(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    img = cv2.fastNlMeansDenoisingColored(img, None, h=10, hColor=10, templateWindowSize=7, searchWindowSize=21)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(l)
    img = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(img, (0, 0), 3)
    return cv2.addWeighted(img, 1.5, blur, -0.5, 0)

def enhance_tile(tile: np.ndarray) -> np.ndarray:
    h, w = tile.shape[:2]
    tile = cv2.resize(tile, (w * 4, h * 4), interpolation=cv2.INTER_LANCZOS4)
    tile = cv2.fastNlMeansDenoisingColored(tile, None, h=12, hColor=12, templateWindowSize=7, searchWindowSize=21)
    lab = cv2.cvtColor(tile, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(4, 4)).apply(l)
    tile = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(tile, (0, 0), 2)
    return cv2.addWeighted(tile, 1.6, blur, -0.6, 0)

def get_tile(img: np.ndarray, idx: int, grid: int) -> np.ndarray:
    h, w = img.shape[:2]
    th, tw = h // grid, w // grid
    r, c = idx // grid, idx % grid
    return img[r*th:(r+1)*th, c*tw:(c+1)*tw].copy()

def get_all_tiles(img: np.ndarray, grid: int) -> dict:
    return {i: get_tile(img, i, grid) for i in range(grid * grid)}

def screenshot_iframe(page) -> bytes | None:
    try:
        el = page.locator('iframe[src*="/recaptcha/api2/bframe"]')
        el.wait_for(state="visible", timeout=12000)
        return el.screenshot(type="png")
    except Exception as e:
        log(f"⚠️ Captura falhou: {e}")
        return None

def bytes_to_cv(b: bytes) -> np.ndarray:
    return cv2.cvtColor(np.array(Image.open(io.BytesIO(b)).convert("RGB")), cv2.COLOR_RGB2BGR)

def crop_grid(img: np.ndarray, bframe) -> np.ndarray:
    try:
        box = bframe.locator("table.rc-imageselect-table").first.bounding_box(timeout=5000)
        if box:
            x, y = max(0, int(box["x"])), max(0, int(box["y"]))
            w, h = int(box["width"]), int(box["height"])
            c = img[y:y+h, x:x+w]
            if c.size > 0:
                return c
    except Exception:
        pass
    return img

def get_question(bframe) -> str | None:
    try:
        bframe.locator("body").wait_for(state="visible", timeout=15000)
        for sel in [
            "div.rc-imageselect-desc-no-canonical strong",
            "div.rc-imageselect-desc strong",
            "div.rc-imageselect-desc",
            "strong"
        ]:
            loc = bframe.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text(timeout=8000).strip()
                if txt:
                    return txt
    except Exception as e:
        log(f"⚠️ Erro pergunta: {e}")
    return None

def click_tile(bframe, idx: int) -> bool:
    try:
        cells = bframe.locator("td.rc-imageselect-tile").all()
        if idx < len(cells):
            cells[idx].scroll_into_view_if_needed(timeout=2000)
            cells[idx].click(delay=random.randint(80, 150))
            return True
    except Exception as e:
        log(f"  ⚠️ Erro clique tile {idx}: {e}")
    return False

def _wait_grid_stable(bframe, page, timeout_ms: int = 8000) -> int:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            bframe.locator("td.rc-imageselect-tile").first.wait_for(state="visible", timeout=3000)
            total = bframe.locator("td.rc-imageselect-tile").count()
            grid_n = {9: 3, 16: 4}.get(total, 0)
            if grid_n > 0:
                return grid_n
            log(f"  ⏳ Grid em transição ({total} tiles) — aguardando...")
            page.wait_for_timeout(400)
        except Exception:
            page.wait_for_timeout(300)
    return 0

def solve(page) -> bool:
    bframe = page.frame_locator('iframe[src*="/recaptcha/api2/bframe"]')
    question = get_question(bframe)
    if not question:
        log("❌ Pergunta não detectada.")
        return False

    _, _, obj_key = dynamic_classifier.classify(question)
    cls_key = obj_key
    log(f"🧠 Objeto: '{obj_key}'")

    session_tiles: dict[int, np.ndarray] = {}
    session_clicked: set[int] = set()

    for passe in range(1, 6):
        log(f"\n── Passe {passe} ──────────────────────────────────────────")
        grid_n = _wait_grid_stable(bframe, page)
        if grid_n == 0:
            log("❌ Grid não estabilizou — abortando")
            return False

        page.wait_for_timeout(150 if passe > 1 else 300)
        img_bytes = screenshot_iframe(page)
        if not img_bytes:
            return False

        grid_cv = crop_grid(bytes_to_cv(img_bytes), bframe)
        session_tiles.update(get_all_tiles(grid_cv, grid_n))
        log(f"  🧠 Vision analisando grid {grid_n}×{grid_n}...")
        found = vision_solver.analyze_grid(grid_cv, obj_key, grid_n)

        if not found:
            if not session_clicked:
                log("  ⏭️ Nenhum objeto → Pular")
                click_skip(bframe)
                page.wait_for_timeout(500)
                return False
            else:
                log("  ✅ Sem mais objetos → Verificar")
                click_verify(bframe)
                page.wait_for_timeout(700)
                if not modal_open(page):
                    log("✅ Captcha resolvido!")
                    memory.commit(session_tiles, session_clicked, cls_key)
                    return True
                new_q = get_question(bframe)
                if new_q and new_q.lower().strip() != question.lower().strip():
                    log(f"  Nova pergunta: '{new_q}' → reiniciando")
                    return False
                log("  ⚠️ Verificar rejeitado — re-analisando")
                session_clicked = set()
                page.wait_for_timeout(600)
                continue

        log(f"  🧠 Tiles encontrados: {found} — clicando...")
        for idx in found:
            if click_tile(bframe, idx):
                session_clicked.add(idx)
                log(f"    [tile {idx}] ✓")
                page.wait_for_timeout(random.randint(60, 120))

        page.wait_for_timeout(300)
        try:
            bframe.locator("td.rc-imageselect-tile.rc-imageselect-dynamic-selected").first.wait_for(state="hidden", timeout=700)
        except Exception:
            pass

        page.wait_for_timeout(100)
        if grid_n == 3 and passe < 5:
            page.wait_for_timeout(400)
            img2 = screenshot_iframe(page)
            if img2:
                grid2 = crop_grid(bytes_to_cv(img2), bframe)
                log("  🔍 Checando novos tiles dinâmicos (3×3)...")
                found2 = vision_solver.analyze_grid(grid2, obj_key, grid_n)
                new_tiles = [i for i in found2 if i not in session_clicked]
                if new_tiles:
                    log(f"  🆕 Novos tiles com objeto: {new_tiles} — próximo passe")
                    continue
                log("  ✓ Sem novos objetos nas imagens substituídas")

        log("  ✅ Verificar/Avançar")
        click_verify(bframe)
        page.wait_for_timeout(700)
        if not modal_open(page):
            log("✅ Captcha resolvido!")
            memory.commit(session_tiles, session_clicked, cls_key)
            return True

        new_q = get_question(bframe)
        if new_q and new_q.lower().strip() != question.lower().strip():
            log(f"  Nova pergunta: '{new_q}' → reiniciando")
            return False

        log("  ⚠️ Verificar rejeitado — aguardando novo grid...")
        session_clicked = set()
        page.wait_for_timeout(700)

    log("❌ Limite de passes atingido.")
    return False

def click_verify(bframe) -> bool:
    try:
        btn = bframe.get_by_role("button", name=re.compile(r"verificar|confirmar|verify|confirm|avançar|submit|ok", re.I)).first
        btn.wait_for(state="visible", timeout=5000)
        btn.click(delay=random.randint(150, 300))
        log("✅ Verificar/Avançar clicado.")
        return True
    except Exception as e:
        log(f"⚠️ Verificar não encontrado: {e}")
    return False

def click_skip(bframe) -> bool:
    for get_btn in [
        lambda: bframe.get_by_role("button", name=re.compile(r"pular|skip", re.I)).first,
        lambda: bframe.locator("button:has-text('Pular')").first,
        lambda: bframe.locator("button:has-text('Skip')").first,
    ]:
        try:
            btn = get_btn()
            if btn.is_visible(timeout=1000):
                btn.click(delay=random.randint(100, 200))
                log("⏭️ Pular clicado.")
                return True
        except Exception:
            pass
    return False

def modal_open(page) -> bool:
    try:
        return page.locator('iframe[src*="/recaptcha/api2/bframe"]').is_visible(timeout=1500)
    except Exception:
        return False

def ensure_recaptcha(page, ms=25000) -> bool:
    try:
        page.wait_for_selector('iframe[src*="recaptcha/api2/anchor"]', timeout=ms)
        log("✅ reCAPTCHA carregou.")
        return True
    except Exception:
        log("ℹ️ reCAPTCHA não apareceu.")
        return False

def smart_field(page, sels):
    for s in sels:
        try:
            loc = page.locator(s)
            if loc.count() > 0 and loc.first.is_visible() and loc.first.is_enabled():
                return loc.first
        except Exception:
            pass
    return None

def smart_user(page):
    return smart_field(page, [
        'input[placeholder*="matrícula" i]', 'input[aria-label*="matrícula" i]',
        'input[placeholder*="usuário" i]', 'input[name*="user" i]',
        'input[name*="login" i]', 'input[id*="user" i]',
        'input[id*="login" i]', 'input[type="text"]',
    ])

def smart_pass(page):
    return smart_field(page, [
        'input[type="password"]', 'input[placeholder*="senha" i]',
        'input[name*="senha" i]', 'input[id*="senha" i]',
    ])

def handle_captcha(page):
    try:
        try:
            page.wait_for_selector('iframe[src*="recaptcha/api2/anchor"]', timeout=8000)
        except Exception:
            log("ℹ️ reCAPTCHA não apareceu — pulando etapa de captcha.")
            return

        anchor = page.frame_locator('iframe[src*="recaptcha/api2/anchor"]')
        checkbox = anchor.locator("#recaptcha-anchor")
        checkbox.wait_for(state="visible", timeout=10000)
        if checkbox.get_attribute("aria-checked") != "true":
            log("Clicando checkbox...")
            checkbox.click(delay=400)
            try:
                page.wait_for_selector('iframe[src*="/recaptcha/api2/bframe"]', timeout=6000)
            except Exception:
                pass
            page.wait_for_timeout(400)

        for attempt in range(1, 31):
            log(f"── Tentativa {attempt}/30 ──")
            try:
                page.wait_for_selector('iframe[src*="/recaptcha/api2/bframe"]', timeout=20000)
                log("Desafio detectado!")
                if solve(page):
                    break
                page.wait_for_timeout(random.randint(500, 1000))
            except PWTimeout:
                log("Modal sumiu (captcha aceito?).")
                break

        if modal_open(page):
            log("❌ Falha → resolução manual")
            input("Resolva manualmente e pressione ENTER...")

        try:
            page.wait_for_selector('iframe[src*="/recaptcha/api2/bframe"]', state="hidden", timeout=90000)
        except Exception:
            pass

        log("✅ reCAPTCHA concluído.")
    except Exception as e:
        log(f"❌ Erro captcha: {e}")

def click_btn(page, labels) -> bool:
    for label in labels:
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).click(timeout=15000)
            log(f"✅ Clicou '{label}'")
            return True
        except Exception:
            pass
    return False

def _human_type(field, text: str, page):
    field.click()
    page.wait_for_timeout(random.randint(200, 400))
    try:
        field.fill("")
    except Exception:
        pass
    for char in text:
        field.type(char, delay=random.randint(60, 180))
    page.wait_for_timeout(random.randint(200, 500))

def _move_mouse_randomly(page):
    for _ in range(random.randint(2, 4)):
        x = random.randint(100, 900)
        y = random.randint(100, 600)
        page.mouse.move(x, y)
        page.wait_for_timeout(random.randint(80, 200))

# =============================================================================
# FUNÇÕES PARA CÓDIGO DE VERIFICAÇÃO (SUPABASE)
# =============================================================================


def buscar_codigo_supabase() -> str:
    """
    Consulta o Supabase e retorna o código mais recente (campo 'codigo').
    Retorna string vazia se falhar.
    """
    url = "https://itvhqqifrhszfejkails.supabase.co/rest/v1/codigos?select=*&order=id.desc&limit=1"
    headers = {
        "apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Iml0dmhxcWlmcmhzemZlamthaWxzIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTE3MDc1MywiZXhwIjoyMDkwNzQ2NzUzfQ.AVzllovUS1NRgWr9_bUxYgjXAjDl4TIFTyHqLUnkirw",
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Iml0dmhxcWlmcmhzemZlamthaWxzIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTE3MDc1MywiZXhwIjoyMDkwNzQ2NzUzfQ.AVzllovUS1NRgWr9_bUxYgjXAjDl4TIFTyHqLUnkirw"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        dados = response.json()
        if dados and isinstance(dados, list) and len(dados) > 0:
            codigo = dados[0].get('codigo', '')
            codigo_str = str(codigo).strip()
            log(f"  ✅ Código obtido do Supabase: {codigo_str}")
            return codigo_str
        else:
            log("  ⚠️ Nenhum código encontrado no Supabase")
            return ""
    except Exception as e:
        log(f"  ❌ Erro ao buscar código no Supabase: {e}")
        return ""

def tela_codigo_esta_visivel(page) -> bool:
    """
    Verifica se a tela de código de verificação está visível.
    """
    try:
        texto_tela = page.locator("body").inner_text(timeout=2000)
        texto_norm = _norm(texto_tela)
        return (
            "codigo de verificacao da matricula" in texto_norm or
            "codigo de verificacao" in texto_norm
        )
    except Exception:
        return False

def esperar_codigo_ou_senha(page, timeout_ms=15000) -> str:
    """
    Após clicar em Avançar, espera condicionalmente pela tela de código
    OU pela tela de senha.
    Retorna: "codigo", "senha" ou "desconhecido"
    """
    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        try:
            if tela_codigo_esta_visivel(page):
                return "codigo"

            pwd = smart_pass(page)
            if pwd and pwd.is_visible():
                return "senha"
        except Exception:
            pass

        page.wait_for_timeout(500)

    return "desconhecido"

def handle_codigo_verificacao(page, espera_inicial=60, tentativas=6, intervalo=5) -> bool:
    """
    Trata a tela de código de verificação somente quando ela estiver visível.
    Fluxo:
        - detecta a tela
        - aguarda 60 segundos
        - consulta o Supabase
        - preenche o campo
        - clica em Avançar
    """
    log("🔐 Verificando se a tela de código de verificação apareceu...")
    try:
        if not tela_codigo_esta_visivel(page):
            log("  ℹ️ Tela de código não apareceu. Seguindo fluxo.")
            return False

        log("  ✅ Tela de código detectada.")
        log(f"  ⏳ Aguardando {espera_inicial} segundos antes de consultar o código...")
        page.wait_for_timeout(espera_inicial * 1000)

        codigo = ""
        for tentativa in range(1, tentativas + 1):
            log(f"  🔎 Consultando código no Supabase ({tentativa}/{tentativas})...")
            codigo = buscar_codigo_supabase()
            if codigo:
                break
            if tentativa < tentativas:
                log(f"  ⏳ Código ainda não encontrado. Aguardando {intervalo}s para nova tentativa...")
                page.wait_for_timeout(intervalo * 1000)

        if not codigo:
            log("  ❌ Código não encontrado no Supabase.")
            return False

        campo_codigo = None
        seletores = [
            'input[type="text"]',
            'input[name*="codigo" i]',
            'input[id*="codigo" i]',
            'input[placeholder*="código" i]',
            'input[placeholder*="codigo" i]',
        ]

        for seletor in seletores:
            try:
                loc = page.locator(seletor).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    campo_codigo = loc
                    log(f"  ✅ Campo de código encontrado via seletor: {seletor}")
                    break
            except Exception:
                continue

        if not campo_codigo:
            log("  ❌ Campo de código não encontrado.")
            return False

        _human_type(campo_codigo, codigo, page)
        log("  ✅ Código preenchido com sucesso.")

        if not click_btn(page, ["Avançar", "Próximo", "Continuar", "Verificar"]):
            log("  ❌ Botão para avançar após o código não encontrado.")
            return False

        page.wait_for_timeout(3000)
        log("  ✅ Fluxo de código concluído.")
        return True

    except Exception as e:
        log(f"  ❌ Erro ao tratar código de verificação: {e}")
        return False

# =============================================================================
# Navegação pós-login
# =============================================================================

def selecionar_carteira(page, carteira: str = None) -> bool:
    carteira = carteira or CARTEIRA
    log(f"🗂️  Selecionando carteira: '{carteira}'")
    try:
        page.wait_for_selector("select", timeout=20000)
        page.wait_for_timeout(random.randint(600, 1000))

        sel = page.locator("select").first
        sel.select_option(label=carteira)
        log(f"  ✅ '{carteira}' selecionado no dropdown")
        page.wait_for_timeout(random.randint(400, 700))

        arrow = None
        for locator_str in [
            "select + button",
            "select ~ button",
            "button.btn-primary",
            "button[type='submit']",
        ]:
            try:
                loc = page.locator(locator_str).first
                if loc.count() > 0 and loc.is_visible(timeout=1500):
                    arrow = loc
                    log(f"  ✅ Seta azul encontrada via: {locator_str}")
                    break
            except Exception:
                continue

        if arrow is None:
            for btn in page.locator("button").all():
                try:
                    if btn.is_visible(timeout=500):
                        arrow = btn
                        log("  ✅ Seta azul: primeiro botão visível (fallback)")
                        break
                except Exception:
                    continue

        if arrow is None:
            log("  ❌ Seta azul não encontrada")
            return False

        arrow.scroll_into_view_if_needed(timeout=3000)
        arrow.click(delay=random.randint(200, 400))
        log("  ✅ Seta azul clicada → aguardando próxima tela...")
        page.wait_for_timeout(random.randint(2000, 3000))
        return True

    except Exception as e:
        log(f"  ❌ Erro ao selecionar carteira: {e}")
        return False

def navegar_pesquisar_contrato(page) -> bool:
    log("🔍 Clicando na seta (=>) da barra azul...")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(random.randint(600, 1000))

        seletores_seta = [
            "header button:first-child",
            "header a:first-child",
            "nav button:first-child",
            ".navbar button:first-child",
            ".navbar-header button",
            "button.navbar-toggle",
            "button.menu-toggle",
            "button[aria-label*='menu' i]",
            "button[aria-label*='navegar' i]",
            "button[aria-label*='voltar' i]",
            ".header button", ".header a",
            ".top-bar button", ".top-bar a",
            "body > div:first-child button",
            "body > header button",
        ]

        for sel in seletores_seta:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    loc.scroll_into_view_if_needed(timeout=2000)
                    loc.click(delay=random.randint(150, 300))
                    log(f"  ✅ Seta clicada via: {sel}")
                    page.wait_for_timeout(random.randint(1000, 1800))
                    return True
            except Exception:
                continue

        log("  ⚠️  Seletores CSS falharam — tentando clique por posição (x=30, y=30)...")
        try:
            page.mouse.click(30, 30, delay=random.randint(150, 300))
            page.wait_for_timeout(random.randint(800, 1200))
            log("  ✅ Clique por posição realizado")
            return True
        except Exception as e:
            log(f"  ⚠️  Clique por posição falhou: {e}")

        log(f"  ❌ Seta não encontrada. URL: {page.url}")
        return False

    except Exception as e:
        log(f"  ⚠️  Erro ao navegar: {e}")
        return False

# =============================================================================
# Helpers robustos para a pop-up "Incluir Imóvel"
# =============================================================================

def _popup_list_fields(popup):
    js = r'''
    () => {
      function visible(el) {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.display !== 'none' && st.visibility !== 'hidden' &&
               r.width > 0 && r.height > 0;
      }
      function norm(s) {
        return (s || '')
          .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
          .toLowerCase().replace(/[:\n\r\t]+/g, ' ')
          .replace(/\s+/g, ' ').trim();
      }
      function inferRowLabel(el) {
        const cell = el.closest('td,th');
        const tr = el.closest('tr');
        if (!cell || !tr) return '';
        const cells = Array.from(tr.children).filter(x => /^(TD|TH)$/.test(x.tagName));
        const idx = cells.indexOf(cell);
        for (let i = idx - 1; i >= 0; i--) {
          const txt = norm(cells[i].innerText || cells[i].textContent || '');
          if (txt) return txt;
        }
        return '';
      }
      function inferLabel(el) {
        const fromRow = inferRowLabel(el);
        if (fromRow) return fromRow;

        const id = el.id || '';
        if (id) {
          const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
          if (lbl) {
            const txt = norm(lbl.innerText || lbl.textContent || '');
            if (txt) return txt;
          }
        }

        return norm(
          el.getAttribute('aria-label') ||
          el.getAttribute('title') ||
          el.getAttribute('placeholder') ||
          el.name ||
          el.id ||
          ''
        );
      }

      const controls = Array.from(document.querySelectorAll('input,select,textarea')).map((el, idx) => {
        const type = (el.type || el.tagName || '').toLowerCase();
        const val = 'value' in el ? (el.value || '') : '';
        return {
          index: idx,
          tag: el.tagName,
          type,
          id: el.id || '',
          name: el.name || '',
          value: val,
          visible: visible(el),
          disabled: !!el.disabled,
          label_guess: inferLabel(el),
        };
      });
      return controls;
    }
    '''
    try:
        return popup.locator("input, select, textarea").evaluate_all(js)
    except Exception as e:
        log(f"  ⚠️ Erro ao listar campos da popup: {e}")
        return []

def _popup_debug_fields(popup):
    log("  🔍 DEBUG: listando campos disponíveis na pop-up...")
    fields = _popup_list_fields(popup)
    log(f"  🔍 Total de inputs/selects/textarea: {len(fields)}")
    for i, f in enumerate(fields):
        log(
            f"    [{i}] {f.get('tag','?')} "
            f"name='{f.get('name','')}' id='{f.get('id','')}' type='{f.get('type','')}' "
            f"visible={f.get('visible')} disabled={f.get('disabled')} "
            f"label='{f.get('label_guess','')}' value='{f.get('value','')}'"
        )

def _popup_find_control_index(popup, aliases, allowed_tags=None, allowed_types=None, exclude_types=None, only_visible=True):
    aliases = [_norm(a) for a in aliases]
    fields = _popup_list_fields(popup)

    best = None
    best_score = 10**9

    for f in fields:
        tag = (f.get("tag") or "").lower()
        typ = (f.get("type") or "").lower()
        label = _norm(f.get("label_guess") or "")
        name = _norm(f.get("name") or "")
        elem_id = _norm(f.get("id") or "")
        value = _norm(f.get("value") or "")

        if only_visible and not f.get("visible"):
            continue
        if f.get("disabled"):
            continue
        if allowed_tags and tag not in [t.lower() for t in allowed_tags]:
            continue
        if allowed_types and typ not in [t.lower() for t in allowed_types]:
            continue
        if exclude_types and typ in [t.lower() for t in exclude_types]:
            continue

        score = None
        for alias in aliases:
            if not alias:
                continue
            if label == alias:
                score = 0
                break
            if alias in label:
                score = 1
                break
            if alias in name or alias in elem_id:
                score = 2
                break
            if alias in value:
                score = 3
                break

        if score is not None and score < best_score:
            best = f["index"]
            best_score = score

    return best

def _popup_locator_by_index(popup, index):
    if index is None:
        return None
    return popup.locator("input, select, textarea").nth(index)

def _popup_human_fill(popup, locator, text: str):
    locator.scroll_into_view_if_needed(timeout=2000)
    locator.click(timeout=3000)
    popup.wait_for_timeout(random.randint(100, 250))
    try:
        locator.fill("")
    except Exception:
        pass
    for ch in str(text):
        locator.type(ch, delay=random.randint(40, 110))
    popup.wait_for_timeout(random.randint(150, 300))

def _popup_fill_field(popup, aliases, value: str, description=None):
    idx = _popup_find_control_index(
        popup,
        aliases=aliases,
        allowed_tags=["input", "textarea"],
        exclude_types=["hidden", "button", "submit", "image", "checkbox", "radio"]
    )
    if idx is None:
        log(f"    ❌ {description or aliases[0]} não encontrado")
        return False
    try:
        loc = _popup_locator_by_index(popup, idx)
        _popup_human_fill(popup, loc, value)
        log(f"    ✅ {description or aliases[0]} preenchido")
        return True
    except Exception as e:
        log(f"    ❌ Erro ao preencher {description or aliases[0]}: {e}")
        return False

def _popup_get_valid_options(select_locator):
    opcoes = []
    try:
        for opt in select_locator.locator("option").all():
            try:
                label = (opt.inner_text(timeout=300) or "").strip()
                value = opt.get_attribute("value") or ""
                nl = _norm(label)
                if not label:
                    continue
                if any(x in nl for x in ["selecione", "selecionar", "escolha", "todos", "todas"]):
                    continue
                opcoes.append((label, value))
            except Exception:
                continue
    except Exception:
        pass
    return opcoes

def _popup_select_random_valid(popup, aliases, description=None):
    idx = _popup_find_control_index(
        popup,
        aliases=aliases,
        allowed_tags=["select"]
    )
    if idx is None:
        log(f"    ⚠️ {description or aliases[0]} não encontrado")
        return False
    try:
        loc = _popup_locator_by_index(popup, idx)
        opcoes = _popup_get_valid_options(loc)
        if not opcoes:
            log(f"    ⚠️ {description or aliases[0]} encontrado, mas sem opções úteis")
            return False
        chosen_label, chosen_value = random.choice(opcoes)
        loc.select_option(value=chosen_value)
        popup.wait_for_timeout(300)
        try:
            loc.dispatch_event("change")
        except Exception:
            pass
        log(f"    ✅ {description or aliases[0]} selecionado: '{chosen_label}'")
        return True
    except Exception as e:
        log(f"    ⚠️ Erro ao selecionar {description or aliases[0]}: {e}")
        return False

def _popup_set_checkbox(popup, aliases, checked=True, description=None):
    idx = _popup_find_control_index(
        popup,
        aliases=aliases,
        allowed_tags=["input"],
        allowed_types=["checkbox"]
    )
    if idx is None:
        log(f"    ⚠️ {description or aliases[0]} não encontrado")
        return False
    try:
        loc = _popup_locator_by_index(popup, idx)
        try:
            current = loc.is_checked()
        except Exception:
            current = False

        if current != checked:
            try:
                loc.check() if checked else loc.uncheck()
            except Exception:
                try:
                    loc.click(delay=120)
                except Exception:
                    pass

        try:
            final = loc.is_checked()
        except Exception:
            final = False

        if final != checked:
            try:
                loc.evaluate(
                    "(el, checked) => { el.checked = checked; el.dispatchEvent(new Event('change', {bubbles:true})); el.dispatchEvent(new Event('input', {bubbles:true})); }",
                    checked
                )
                final = loc.is_checked()
            except Exception:
                pass

        if final == checked:
            log(f"    ✅ {description or aliases[0]} {'marcado' if checked else 'desmarcado'}")
            return True

        log(f"    ⚠️ Não foi possível garantir o estado do checkbox {description or aliases[0]}")
        return False
    except Exception as e:
        log(f"    ⚠️ Erro ao ajustar checkbox {description or aliases[0]}: {e}")
        return False

def _popup_click_button(popup, aliases, description=None):
    aliases = [_norm(a) for a in aliases]
    fields = _popup_list_fields(popup)

    candidates = []
    for f in fields:
        typ = (f.get("type") or "").lower()
        tag = (f.get("tag") or "").lower()
        visible = f.get("visible")
        disabled = f.get("disabled")
        if not visible or disabled:
            continue
        if tag == "input" and typ not in ["button", "submit", "image"]:
            continue
        hay = _norm(" ".join([
            f.get("label_guess") or "",
            f.get("name") or "",
            f.get("id") or "",
            f.get("value") or "",
        ]))
        for alias in aliases:
            if alias and alias in hay:
                candidates.append(f["index"])
                break

    for idx in candidates:
        try:
            loc = _popup_locator_by_index(popup, idx)
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(delay=150)
            log(f"    ✅ {description or aliases[0]} clicado")
            return True
        except Exception:
            continue

    for alias in aliases:
        try:
            btn = popup.get_by_role("button", name=re.compile(alias, re.I)).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                btn.click(delay=150)
                log(f"    ✅ {description or alias} clicado")
                return True
        except Exception:
            pass

    log(f"    ⚠️ {description or aliases[0]} não encontrado")
    return False

def _popup_wait_non_empty(popup, aliases, description=None, timeout_ms=8000):
    idx = _popup_find_control_index(
        popup,
        aliases=aliases,
        allowed_tags=["input", "textarea", "select"],
        exclude_types=["hidden", "button", "submit", "image", "checkbox", "radio"]
    )
    if idx is None:
        log(f"    ⚠️ Campo {description or aliases[0]} não encontrado para aguardar")
        return False
    try:
        loc = _popup_locator_by_index(popup, idx)
        popup.wait_for_function(
            """el => {
                if (!el) return false;
                if (el.tagName === 'SELECT') return (el.value || '').trim() !== '';
                return (el.value || '').trim() !== '';
            }""",
            arg=loc.element_handle(),
            timeout=timeout_ms
        )
        log(f"    ✅ {description or aliases[0]} preenchido automaticamente")
        return True
    except Exception as e:
        log(f"    ⚠️ Tempo esgotado para {description or aliases[0]}: {e}")
        return False

def _popup_contains_duplicate_warning(popup) -> bool:
    try:
        body_text = _norm(popup.locator("body").inner_text(timeout=1000))
        return "este imovel ja esta cadastrado" in body_text
    except Exception:
        return False

def preencher_popup_imovel(popup) -> bool:
    log("📌 Preenchendo pop-up 'Incluir Imóvel'...")
    try:
        popup.wait_for_load_state("domcontentloaded", timeout=15000)
        popup.wait_for_timeout(1200)

        try:
            popup.wait_for_selector("form, table", timeout=8000)
        except Exception:
            pass

        _popup_debug_fields(popup)

        dados = gerar_dados_ficticios_imovel()
        log(f"  🎲 Dados fictícios desta execução: {dados}")

        # 1. Matrícula do imóvel (com tentativas para evitar duplicado)
        matricula_ok = False
        for tentativa in range(1, 6):
            if tentativa > 1:
                dados["matricula"] = _random_digits(random.randint(7, 10), first_non_zero=True)
                log(f"    🔄 Nova matrícula fictícia tentativa {tentativa}: {dados['matricula']}")

            _popup_fill_field(
                popup,
                aliases=["matricula do imovel", "matricula imovel", "matricula"],
                value=dados["matricula"],
                description="Matrícula do Imóvel"
            )
            popup.wait_for_timeout(400)

            if not _popup_contains_duplicate_warning(popup):
                matricula_ok = True
                break

            log("    ⚠️ Matrícula já cadastrada detectada na tela. Gerando nova matrícula...")

        if not matricula_ok:
            log("    ❌ Não foi possível gerar uma matrícula fictícia não cadastrada")
            return False

        # 2. CEP
        cep_ok = _popup_fill_field(
            popup,
            aliases=["cep"],
            value=dados["cep"],
            description="CEP"
        )

        # 3. Buscar
        if cep_ok:
            _popup_click_button(
                popup,
                aliases=["buscar"],
                description="Botão 'Buscar'"
            )
            popup.wait_for_timeout(1200)

        # 4. Aguarda preenchimento automático do endereço
        log("    ⏳ Aguardando preenchimento automático dos dados do CEP...")
        _popup_wait_non_empty(popup, ["municipio"], "Município", timeout_ms=8000)
        _popup_wait_non_empty(popup, ["uf"], "UF", timeout_ms=8000)
        _popup_wait_non_empty(popup, ["tipo logradouro", "tipo"], "Tipo Logradouro", timeout_ms=8000)
        _popup_wait_non_empty(popup, ["logradouro"], "Logradouro", timeout_ms=8000)
        _popup_wait_non_empty(popup, ["bairro"], "Bairro", timeout_ms=8000)

        popup.wait_for_timeout(600)

        # 5. Número
        _popup_fill_field(
            popup,
            aliases=["numero"],
            value=dados["numero"],
            description="Número"
        )

        # 6. Tipo de Complemento (aleatório)
        _popup_select_random_valid(
            popup,
            aliases=["tipo de complemento", "tipo complemento"],
            description="Tipo de Complemento"
        )

        # 7. Número do Complemento
        _popup_fill_field(
            popup,
            aliases=["numero do complemento", "número do complemento", "complemento"],
            value=dados["numero_complemento"],
            description="Número do Complemento"
        )

        # 8. Bloco
        _popup_fill_field(
            popup,
            aliases=["bloco"],
            value=dados["bloco"],
            description="Bloco"
        )

        # 9. Empreendimento: sempre marcado
        _popup_set_checkbox(
            popup,
            aliases=["empreendimento", "chkempreendimento"],
            checked=True,
            description="Empreendimento"
        )

        popup.wait_for_timeout(800)

        # 10. Clicar em incluir
        log("🚀 Clicando em 'incluir'...")
        clicou = False
        try:
            btn = popup.get_by_role("button", name=re.compile(r"incluir", re.I)).first
            if btn.count() > 0 and btn.is_visible(timeout=1000):
                btn.click(delay=180)
                clicou = True
        except Exception:
            pass

        if not clicou:
            try:
                btn = popup.locator(
                    'input[type="button"][value*="incluir" i], '
                    'input[type="submit"][value*="incluir" i], '
                    'button:has-text("incluir")'
                ).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(delay=180)
                    clicou = True
            except Exception:
                pass

        if clicou:
            log("    ✅ Botão 'incluir' clicado")
        else:
            log("    ❌ Botão 'incluir' não encontrado")
            return False

        popup.wait_for_timeout(2000)

        try:
            popup.wait_for_event("close", timeout=5000)
            log("✅ Pop-up fechada automaticamente.")
        except Exception:
            log("⚠️ Pop-up não fechou automaticamente, mas pode estar ok.")

        return True

    except Exception as e:
        log(f"❌ Erro ao preencher pop-up: {e}")
        return False

# =============================================================================
# Funções do menu lateral e SDF
# =============================================================================

def fechar_alerta(page) -> bool:
    try:
        fechar = page.locator('button:has-text("Fechar"), button:has-text("OK"), button:has-text("×"), .modal button').first
        if fechar.is_visible(timeout=3000):
            fechar.click(delay=150)
            log("  ✅ Alerta fechado")
            page.wait_for_timeout(400)
            return True
    except Exception:
        pass
    return False

def _menu_esta_aberto(page) -> bool:
    indicadores = [
        'text="Pesquisar Menu"',
        'text="Consulta de FMPs"',
        'text="FGTS na Moradia Própria"',
        'text="Consultas FGTS"',
        'input[placeholder*="Buscar no menu" i]',
    ]
    for sel in indicadores:
        try:
            if page.locator(sel).first.is_visible(timeout=600):
                return True
        except Exception:
            continue
    return False

def _clicar_icone_menu(page):
    seletores = [
        '[class*="toggle" i]',
        '[class*="hamburger" i]',
        '[id*="toggle" i]',
        '[aria-label*="menu" i]',
        '[title*="menu" i]',
        "nav a:first-child",
        "header a:first-child",
        ".navbar-header a",
        "a:first-child",
        "button:first-child",
    ]
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=500):
                box = loc.bounding_box(timeout=500)
                if box and box["x"] < 80 and box["y"] < 60:
                    loc.click(delay=120)
                    log(f"  ✅ Ícone menu clicado via CSS: {sel}")
                    return True
        except Exception:
            continue

    log("  ↳ CSS sem match — clique por coordenada (x=28, y=30)...")
    page.mouse.click(28, 30, delay=120)
    return True

def abrir_menu_lateral(page) -> bool:
    log("📂 Verificando menu lateral...")

    if _menu_esta_aberto(page):
        log("  ✅ Menu já está aberto — prosseguindo")
        return True

    log("  ↳ Menu fechado — clicando ≡> para abrir...")

    for tentativa in range(1, 4):
        log(f"  🔄 Tentativa {tentativa}/3...")
        _clicar_icone_menu(page)
        page.wait_for_timeout(1200)

        if _menu_esta_aberto(page):
            log("  ✅ Menu aberto com sucesso!")
            return True

        log(f"  ⚠️  Ainda fechado ({tentativa}/3)...")
        page.wait_for_timeout(500)

    log("  ❌ Menu não abriu após 3 tentativas")
    return False

def _get_sdf_frame(page):
    try:
        for f in page.frames:
            url = f.url or ""
            if any(x in url for x in ("recaptcha", "google", "gstatic")):
                continue
            try:
                cnt = f.locator("select").count()
                if cnt >= 1:
                    log(f"  🖼️  Frame com selects: {url[:90]}  ({cnt} selects)")
                    return f
            except Exception:
                continue
    except Exception:
        pass
    return None

def _log_selects(ctx, label: str):
    try:
        selects = ctx.locator("select").all()
        log(f"  🔎 [{label}] {len(selects)} select(s) encontrado(s):")
        for i, s in enumerate(selects):
            try:
                name = s.get_attribute("name") or s.get_attribute("id") or "?"
                opts = [o.inner_text(timeout=300).strip() for o in s.locator("option").all()[:5]]
                log(f"     [{i}] name/id={name!r}  opts={opts}")
            except Exception:
                log(f"     [{i}] (erro ao inspecionar)")
    except Exception as e:
        log(f"  🔎 [{label}] erro: {e}")

def _clicar_select_e_selecionar(sel_elem, txt: str, page) -> bool:
    try:
        sel_elem.scroll_into_view_if_needed(timeout=2000)
        sel_elem.click(delay=150)
        page.wait_for_timeout(400)

        try:
            opt_loc = sel_elem.locator(f'option:has-text("{txt}")')
            if opt_loc.count() > 0:
                sel_elem.select_option(label=txt)
                page.wait_for_timeout(200)
                sel_elem.dispatch_event("change")
                log(f"    ↳ Clicado na option: '{txt}'")
                return True
        except Exception:
            pass

        sel_elem.select_option(label=txt)
        page.wait_for_timeout(200)
        sel_elem.dispatch_event("change")
        sel_elem.press("Enter")
        page.wait_for_timeout(200)
        log(f"    ↳ Selecionado via select_option+Enter: '{txt}'")
        return True

    except Exception as e:
        log(f"    ⚠️  _clicar_select_e_selecionar erro: {e}")
    return False

def _selecionar_por_opcao(ctx, opcao_alvo: str, page) -> bool:
    opcao_lower = opcao_alvo.lower().strip()

    contextos = [ctx]
    try:
        for f in page.frames:
            url = f.url or ""
            if any(x in url for x in ("recaptcha", "google", "gstatic")):
                continue
            if f != ctx:
                contextos.append(f)
    except Exception:
        pass

    for contexto in contextos:
        try:
            selects = contexto.locator("select").all()
            log(f"    🔎 {len(selects)} select(s) no contexto")
            for sel_elem in selects:
                try:
                    opts = sel_elem.locator("option").all()
                    for opt in opts:
                        try:
                            txt = (opt.inner_text(timeout=300) or "").strip()
                            if not txt:
                                continue
                            if (txt.lower() == opcao_lower or
                                opcao_lower[:25] in txt.lower() or
                                txt.lower()[:25] in opcao_lower):
                                if _clicar_select_e_selecionar(sel_elem, txt, page):
                                    return True
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            continue
    return False

def preencher_formulario_sdf(page) -> bool:
    log("📋 Preenchendo formulário SDF...")
    try:
        log("  ⏳ Aguardando tela SDF carregar...")
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        page.wait_for_timeout(random.randint(1500, 2000))

        ctx = _get_sdf_frame(page) or page
        ctx_label = "frame" if ctx != page else "page"
        log(f"  🖼️  Contexto usado: {ctx_label}")
        _log_selects(ctx, ctx_label)

        def aceitar_dialog(dialog):
            log(f"  🔔 Dialog: '{dialog.message[:100]}' → OK")
            try:
                dialog.accept()
            except Exception:
                pass
        page.on("dialog", aceitar_dialog)

        log("  2️⃣  Selecionando 'Forma de Utilização'...")
        opcao_forma = "Aquisição de Imóvel em Construção"
        forma_ok = _selecionar_por_opcao(ctx, opcao_forma, page)

        if not forma_ok:
            log(f"  ❌ Opção '{opcao_forma}' não encontrada nos selects")
            page.remove_listener("dialog", aceitar_dialog)
            return False

        log("  ✅ Forma de Utilização selecionada!")
        page.wait_for_timeout(random.randint(400, 600))

        for sel_ok in [
            'input[type="button"][value="OK"]',
            'input[value="OK"]',
            'button:has-text("OK")',
            'input[type="submit"][value="OK"]',
        ]:
            try:
                for f in [page] + list(page.frames):
                    try:
                        btn = f.locator(sel_ok).first
                        if btn.is_visible(timeout=800):
                            btn.click(delay=150)
                            log(f"  ✅ Popup OK (HTML) fechado: {sel_ok}")
                            page.wait_for_timeout(500)
                            raise StopIteration
                    except StopIteration:
                        raise
                    except Exception:
                        continue
            except StopIteration:
                break
            except Exception:
                continue

        page.wait_for_timeout(50)

        log("  3️⃣  Selecionando 'Âmbito da Operação'...")
        opcao_sfh = "Operações realizadas com financiamento do SFH"

        ctx = _get_sdf_frame(page) or page
        _log_selects(ctx, "pós-dialog")

        ambito_ok = _selecionar_por_opcao(ctx, opcao_sfh, page)
        if ambito_ok:
            log("  ✅ Âmbito da Operação selecionado!")
        else:
            log("  ⚠️  Âmbito não selecionado — continuando mesmo assim...")

        page.wait_for_timeout(300)

        log("  4️⃣  Clicando (...) de Matrícula do Imóvel e capturando pop-up...")
        matricula_clicado = False

        sels_matricula = [
            '#ImagemPopUp',
            'a:has(#ImagemPopUp)',
            'td a:has(div)',
            'input[type="button"][value="..."]',
            'input[type="button"][value="…"]',
            'input[type="image"]',
            'button:has-text("...")',
            'a:has-text("...")',
            'tr:has-text("Matrícula") a',
            'tr:has-text("Matrícula") input[type="button"]',
            'tr:has-text("Matrícula") input[type="image"]',
            'td input[type="button"]',
            'td input[type="image"]',
        ]

        frames_busca = [ctx]
        try:
            for f in page.frames:
                url = f.url or ""
                if any(x in url for x in ("recaptcha", "google", "gstatic")):
                    continue
                if f not in frames_busca:
                    frames_busca.append(f)
        except Exception:
            pass

        popup = None
        try:
            with page.context.expect_page(timeout=10000) as popup_info:
                for frame in frames_busca:
                    if matricula_clicado:
                        break
                    for sel in sels_matricula:
                        try:
                            loc = frame.locator(sel).first
                            if loc.count() > 0 and loc.is_visible(timeout=800):
                                box = loc.bounding_box(timeout=800)
                                if box and box["width"] > 0 and box["height"] > 0:
                                    loc.scroll_into_view_if_needed(timeout=2000)
                                    loc.click(delay=120)
                                    log(f"  ✅ (...) clicado via: {sel} (frame: {(frame.url or '')[:50]})")
                                    matricula_clicado = True
                                    break
                        except Exception:
                            continue
            if matricula_clicado:
                popup = popup_info.value
        except Exception as e:
            log(f"  ⚠️ expect_page falhou: {e}")

        if matricula_clicado and popup:
            try:
                log("  ✅ Pop-up detectada!")
                if preencher_popup_imovel(popup):
                    log("  ✅ Pop-up preenchida e submetida com sucesso!")
                else:
                    log("  ⚠️  Falha ao preencher pop-up — verifique manualmente.")
            except Exception as e:
                log(f"  ⚠️  Erro ao capturar/preencher pop-up: {e}")
        else:
            log("  ↳ Tentando clique via JavaScript...")
            try:
                with page.context.expect_page(timeout=10000) as popup_info:
                    clicked = page.evaluate('''() => {
                        const byId = document.getElementById('ImagemPopUp');
                        if (byId) {
                            const link = byId.closest('a') || byId;
                            link.click();
                            return true;
                        }
                        for (const iframe of document.querySelectorAll('iframe')) {
                            try {
                                const doc = iframe.contentDocument;
                                if (!doc) continue;
                                const el = doc.getElementById('ImagemPopUp');
                                if (el) {
                                    const link = el.closest('a') || el;
                                    link.click();
                                    return true;
                                }
                            } catch(e) {}
                        }
                        return false;
                    }''')
                if clicked:
                    popup = popup_info.value
                    if preencher_popup_imovel(popup):
                        log("  ✅ Pop-up preenchida via JS")
                    else:
                        log("  ⚠️  Falha ao preencher pop-up via JS")
                else:
                    log("  ❌ JS não encontrou o botão")
            except Exception as e:
                log(f"  ⚠️  JS click falhou: {e}")

        try:
            page.remove_listener("dialog", aceitar_dialog)
        except Exception:
            pass

        log("  ✅ Formulário SDF concluído!")
        return True

    except Exception as e:
        log(f"  ❌ Erro ao preencher SDF: {e}")
        return False

def navegar_menu_lateral(page) -> bool:
    log("📋 Navegando menu lateral...")
    try:
        page.wait_for_timeout(random.randint(600, 1000))

        log("  1️⃣  Clicando 'FGTS na Moradia Própria'...")
        passo1 = _clicar_menu(page, [
            'a:has-text("FGTS na Moradia Própria")',
            'li:has-text("FGTS na Moradia Própria")',
            'span:has-text("FGTS na Moradia Própria")',
            ':has-text("FGTS na Moradia Própria")',
        ])
        if not passo1:
            log("  ⚠️  'FGTS na Moradia Própria' não encontrado")
            return False
        page.wait_for_timeout(random.randint(600, 1000))

        log("  2️⃣  Clicando 'Ressarcimento FGTS'...")
        passo2 = _clicar_menu(page, [
            'a:has-text("Ressarcimento FGTS")',
            'li:has-text("Ressarcimento FGTS")',
            'span:has-text("Ressarcimento FGTS")',
            ':has-text("Ressarcimento FGTS")',
        ])
        if not passo2:
            log("  ⚠️  'Ressarcimento FGTS' não encontrado")
            return False
        page.wait_for_timeout(random.randint(600, 1000))

        log("  3️⃣  Clicando 'Solicitar Débito de FGTS'...")
        passo3 = _clicar_menu(page, [
            'a:has-text("Solicitar Débito de FGTS")',
            'li:has-text("Solicitar Débito de FGTS")',
            'span:has-text("Solicitar Débito de FGTS")',
            ':has-text("Solicitar Débito de FGTS")',
        ])
        if not passo3:
            log("  ⚠️  'Solicitar Débito de FGTS' não encontrado")
            return False

        page.wait_for_timeout(random.randint(1500, 2500))
        log(f"  ✅ Menu navegado com sucesso! URL: {page.url}")
        return True

    except Exception as e:
        log(f"  ❌ Erro ao navegar menu: {e}")
        return False

def _clicar_menu(page, seletores: list[str]) -> bool:
    for sel in seletores:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible(timeout=3000):
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(delay=random.randint(120, 250))
                return True
        except Exception:
            continue
    return False

# =============================================================================
# MAIN
# =============================================================================

def main():
    if not CIWEB_USER or not CIWEB_PASS:
        raise ValueError("Preencha CIWEB_USER e CIWEB_PASS no .env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise ValueError("Preencha ANTHROPIC_API_KEY no .env")

    log(f"🧠 Modelo: {ANTHROPIC_MODEL}")
    log(f"🗂️  Carteira: {CARTEIRA}")
    if not HAS_STEALTH:
        log("⚠️ ATENÇÃO: pip install playwright-stealth")

    stealth_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars", "--no-sandbox", "--disable-setuid-sandbox",
        "--disable-dev-shm-usage", "--disable-accelerated-2d-canvas",
        "--no-first-run", "--no-zygote", "--disable-gpu",
        "--window-size=1366,768",
        "--exclude-switches=enable-automation",
        "--disable-extensions-except=",
    ]

    for lock in glob.glob(os.path.join(PROFILE_DIR, "*.lock")) + [
        os.path.join(PROFILE_DIR, "lockfile"),
        os.path.join(PROFILE_DIR, "SingletonLock"),
        os.path.join(PROFILE_DIR, "SingletonSocket"),
        os.path.join(PROFILE_DIR, "SingletonCookie")
    ]:
        if os.path.exists(lock):
            try:
                os.remove(lock)
                log(f"🔓 Lock removido: {lock}")
            except Exception as e:
                log(f"⚠️  Não foi possível remover {lock}: {e}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            args=stealth_args,
            ignore_default_args=["--enable-automation"],
        )

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if HAS_STEALTH:
            stealth_sync(page)
            log("✅ Stealth aplicado.")

        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR','pt','en-US','en'] });
            window.chrome = { runtime: {} };
            const orig = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : orig(p);
        """)
        page.on("requestfailed", lambda r: log(f"❌ Request: {r.url}"))

        try:
            log(f"Abrindo {CIWEB_URL}")
            page.goto(CIWEB_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(random.randint(2000, 4000))
            _move_mouse_randomly(page)
            ensure_recaptcha(page)

            user = smart_user(page)
            if not user:
                raise RuntimeError("Campo usuário não encontrado.")
            _move_mouse_randomly(page)
            _human_type(user, CIWEB_USER, page)
            log("✅ Usuário preenchido")
            page.wait_for_timeout(random.randint(1000, 2000))
            _move_mouse_randomly(page)

            handle_captcha(page)

            if not click_btn(page, ["Avançar", "Próximo", "Continuar"]):
                raise RuntimeError("Botão Avançar não encontrado.")
            page.wait_for_timeout(random.randint(1500, 2500))

            proxima_etapa = esperar_codigo_ou_senha(page, timeout_ms=CODIGO_TIMEOUT_DETECCAO_MS)

            if proxima_etapa == "codigo":
                log("🔐 Tela de código detectada antes da senha.")
                if not handle_codigo_verificacao(
                    page,
                    espera_inicial=CODIGO_ESPERA_INICIAL_SEGUNDOS,
                    tentativas=CODIGO_TENTATIVAS_SUPABASE,
                    intervalo=CODIGO_INTERVALO_CONSULTA_SEGUNDOS,
                ):
                    raise RuntimeError("Falha ao preencher o código de verificação.")

                proxima_etapa = esperar_codigo_ou_senha(page, timeout_ms=CODIGO_TIMEOUT_DETECCAO_MS)
                if proxima_etapa != "senha":
                    raise RuntimeError("Após informar o código, o campo de senha não apareceu.")

            elif proxima_etapa == "senha":
                log("🔑 Tela de senha detectada. Seguindo fluxo normal.")

            else:
                raise RuntimeError("Nem a tela de código nem a tela de senha apareceram após clicar em Avançar.")

            pwd = smart_pass(page)
            if not pwd:
                raise RuntimeError("Campo senha não encontrado.")
            _human_type(pwd, CIWEB_PASS, page)
            log("✅ Senha preenchida")
            page.wait_for_timeout(random.randint(800, 1500))

            if not click_btn(page, ["Entrar", "Acessar", "Login", "Confirmar"]):
                raise RuntimeError("Botão login não encontrado.")
            page.wait_for_timeout(random.randint(4000, 6000))
            log("✅ Login realizado!")

            # Continua o fluxo normal
            if not selecionar_carteira(page, CARTEIRA):
                log("⚠️  Selecione a carteira manualmente.")
                input("Após selecionar e clicar na seta, pressione ENTER...")

            log("⏳ Aguardando página carregar...")
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(random.randint(800, 1200))
            fechar_alerta(page)

            if not abrir_menu_lateral(page):
                log("⚠️  Clique no (≡) manualmente e pressione ENTER...")
                input()

            fechar_alerta(page)
            navegar_menu_lateral(page)
            preencher_formulario_sdf(page)

            log("🎉 Pronto! Formulário SDF preenchido.")
            log(f"   URL: {page.url}")
            input("ENTER para fechar...")

        except Exception as e:
            log(f"❌ Erro geral: {e}")
            input("ENTER para fechar...")
        finally:
            try:
                ctx.close()
            except Exception as e:
                log(f"⚠️ Erro ao fechar (ignorável): {e}")

if __name__ == "__main__":
    main()