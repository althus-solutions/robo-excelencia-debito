from fastapi import FastAPI
import threading
import traceback
import subprocess

app = FastAPI()


# 🔧 Garante que o Chromium esteja instalado (Playwright)
def instalar_playwright():
    try:
        print("🔧 Instalando Chromium (Playwright)...")

        subprocess.run(
            ["python", "-m", "playwright", "install", "chromium"],
            check=True
        )

        print("✅ Chromium pronto")

    except Exception as e:
        print("❌ Erro ao instalar Playwright:", e)


# 🔹 Endpoint base
@app.get("/")
def home():
    return {"status": "API rodando"}


# 🔥 Endpoint principal (chamado pelo n8n)
@app.post("/executar")
def executar():
    try:
        print("🔥 /executar chamado")

        # 🔧 Garante browser antes de rodar
        instalar_playwright()

        # 🔁 Executa em background (não trava HTTP)
        def run():
            try:
                print("🚀 Iniciando robô...")

                from main import main
                main()

                print("✅ Robô finalizado")

            except Exception as e:
                print("❌ Erro no robô:", e)
                print(traceback.format_exc())

        threading.Thread(target=run).start()

        return {"status": "execução iniciada"}

    except Exception as e:
        return {
            "status": "erro",
            "erro": str(e),
            "trace": traceback.format_exc()
        }