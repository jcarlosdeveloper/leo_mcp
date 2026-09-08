#!/usr/bin/env python3
"""
Leo MCP Live Monitor - Monitorea interacciones con Leo en tiempo real
Muestra: latency, error rates, stalls, cache hits, streaming vs blocking
"""

import sqlite3
import os
import sys
import time
from datetime import datetime
from collections import defaultdict

HERMES_DIR = os.path.expanduser("~/.hermes")
METRICS_DB = os.path.join(HERMES_DIR, "leo_metrics.db")
DEBUG_LOG = os.path.join(HERMES_DIR, "mcp_debug.log")
UUID_LOG = os.path.join(HERMES_DIR, "leo_uuids.log")

def tail_log(filepath, lines=10):
    """Lee las últimas N líneas de un log"""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r') as f:
        all_lines = f.readlines()
        return all_lines[-lines:]

def get_recent_metrics(limit=20):
    """Obtiene métricas recientes de la DB"""
    if not os.path.exists(METRICS_DB):
        return []
    
    con = sqlite3.connect(METRICS_DB)
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT ts, skill, prompt_len, duration_ms, status "
        "FROM requests ORDER BY ts DESC LIMIT ?",
        (limit,)
    )
    results = [dict(row) for row in cur.fetchall()]
    con.close()
    return results

def calculate_stats(metrics):
    """Calcula estadísticas básicas"""
    if not metrics:
        return {}
    
    total = len(metrics)
    ok_count = sum(1 for m in metrics if m['status'] == 'ok')
    error_count = total - ok_count
    cache_hits = sum(1 for m in metrics if m['status'] == 'cache_hit')
    
    durations = [m['duration_ms'] for m in metrics if m['duration_ms'] > 0]
    avg_latency = sum(durations) / len(durations) if durations else 0
    max_latency = max(durations) if durations else 0
    min_latency = min(durations) if durations else 0
    
    return {
        'total': total,
        'ok': ok_count,
        'errors': error_count,
        'success_rate': f"{(ok_count/total*100):.1f}%" if total > 0 else "N/A",
        'cache_hits': cache_hits,
        'cache_hit_rate': f"{(cache_hits/total*100):.1f}%" if total > 0 else "N/A",
        'avg_latency_ms': f"{avg_latency:.0f}",
        'min_latency_ms': f"{min_latency:.0f}",
        'max_latency_ms': f"{max_latency:.0f}",
    }

def print_dashboard():
    """Imprime dashboard en tiempo real"""
    os.system('clear' if os.name != 'nt' else 'cls')
    
    print("=" * 80)
    print("🦁 LEO MCP SERVER - LIVE MONITOR 🦁")
    print(f"   Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    
    # Estadísticas
    metrics = get_recent_metrics(50)
    stats = calculate_stats(metrics)
    
    if stats:
        print("\n📊 ESTADÍSTICAS (últimas 50 requests):")
        print(f"   Total Requests:    {stats['total']}")
        print(f"   Success Rate:      {stats['success_rate']} ({stats['ok']} OK, {stats['errors']} errores)")
        print(f"   Cache Hit Rate:    {stats['cache_hit_rate']} ({stats['cache_hits']} hits)")
        print(f"   Latency:           avg={stats['avg_latency_ms']}ms | min={stats['min_latency_ms']}ms | max={stats['max_latency_ms']}ms")
    else:
        print("\n⚠️  No hay métricas disponibles aún (primera ejecución)")
    
    # Últimos logs
    print("\n📝 ÚLTIMOS LOGS (mcp_debug.log):")
    debug_lines = tail_log(DEBUG_LOG, 5)
    for line in debug_lines:
        line = line.strip()
        if 'ERROR' in line or 'WARN' in line:
            print(f"   ⚠️  {line}")
        elif 'STREAM' in line:
            print(f"   📡 {line}")
        else:
            print(f"   {line}")
    
    # Últimos UUIDs
    print("\n🆔 ÚLTIMAS CONVERSACIONES (leo_uuids.log):")
    uuid_lines = tail_log(UUID_LOG, 5)
    for line in uuid_lines:
        print(f"   {line.strip()}")
    
    # Archivos críticos
    print("\n📁 ESTADO DE ARCHIVOS:")
    files_to_check = [
        ("leo_mcp_server.py", "/Users/tlaloc_ai/leo_mcp/leo_mcp_server.py"),
        ("leo_ask.sh", "/Users/tlaloc_ai/leo_mcp/leo_ask.sh"),
        ("leo_read.py", "/Users/tlaloc_ai/leo_mcp/leo_read.py"),
        ("skills.json", "/Users/tlaloc_ai/leo_mcp/skills.json"),
    ]
    
    for name, path in files_to_check:
        exists = os.path.exists(path)
        mtime = os.path.getmtime(path) if exists else 0
        mtime_str = datetime.fromtimestamp(mtime).strftime('%H:%M:%S') if exists else "N/A"
        status = f"✓ modificado {mtime_str}" if exists and mtime > time.time() - 3600 else "✓" if exists else "❌"
        print(f"   {name:20} {status}")
    
    print("\n" + "=" * 80)
    print("💡 TIP: Presiona Ctrl+C para salir. El monitor se actualiza cada 5s.")
    print("=" * 80)

def main():
    """Loop principal de monitoreo"""
    print("🚀 Iniciando Leo MCP Live Monitor...")
    print("Presiona Ctrl+C para salir\n")
    
    try:
        while True:
            print_dashboard()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n\n👋 Monitor detenido por usuario.")
        sys.exit(0)

if __name__ == "__main__":
    main()