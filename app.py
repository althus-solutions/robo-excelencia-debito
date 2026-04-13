from fastapi import FastAPI
import threading
import traceback

app = FastAPI()

@app.get("/")
def home():
    return {"status": "API rodando"}

@app.post("/executar")
def executar():
    try:
        def run():
            try:
                from main import main
                main()
            except Exception as e:
                print("Erro no robô:", e)

        threading.Thread(target=run).start()

        return {"status": "execução iniciada"}

    except Exception as e:
        return {
            "status": "erro",
            "erro": str(e),
            "trace": traceback.format_exc()
        }