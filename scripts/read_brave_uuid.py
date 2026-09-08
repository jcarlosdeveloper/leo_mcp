#!/usr/bin/env python3
"""
Leer el UUID de la conversación actual de Brave AI Chat directamente desde SQLite
Sin abrir navegador, sin AppleScript, sin ventanas.
"""
import sqlite3
import os
from pathlib import Path

# Ruta de la base de datos de Brave AI Chat
DB_PATH = Path.home() / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser" / "Default" / "AIChat"

def read_uuid_from_db() -> dict:
    """
    Lee el UUID de la conversación más reciente de Brave AI Chat.
    Usa URI con mode=readonly para evitar locks.
    """
    if not DB_PATH.exists():
        return {
            "success": False,
            "error": f"Database not found: {DB_PATH}",
        }
    
    try:
        # URI con mode=ro (readonly) y immutable=true para evitar locks
        uri = f"file:{DB_PATH}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=1)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Descubrir tablas
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
        
        print(f"📊 Tablas en la DB: {tables}")
        print()
        
        # Explorar cada tabla
        for table in tables:
            print(f"📋 Tabla: {table}")
            
            # Obtener columnas
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            print(f"   Columnas: {columns}")
            
            # Obtener count
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            print(f"   Rows: {count}")
            
            # Mostrar últimas filas (las más recientes primero si hay ID/timestamp)
            if count > 0:
                print(f"   Últimos 5 registros:")
                cursor.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 5")
                rows = cursor.fetchall()
                for row in rows:
                    print(f"     {dict(row)}")
            print()
        
        # Buscar UUID en tablas que parezcan de conversaciones
        uuid_candidates = []
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [col[1] for col in cursor.fetchall()]
            
            # Buscar columnas que puedan contener UUID
            uuid_cols = [c for c in columns if 'uuid' in c.lower() or 'id' in c.lower() or 'conversation' in c.lower()]
            
            if uuid_cols:
                for col in uuid_cols:
                    try:
                        cursor.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY rowid DESC LIMIT 5")
                        values = [row[0] for row in cursor.fetchall() if row[0]]
                        if values:
                            uuid_candidates.append({
                                "table": table,
                                "column": col,
                                "values": values[:3],  # Primeros 3
                            })
                    except:
                        pass
        
        conn.close()
        
        if uuid_candidates:
            print("🎯 Candidatos a UUID:")
            for candidate in uuid_candidates:
                print(f"   {candidate['table']}.{candidate['column']}: {candidate['values']}")
            
            # Asumir que el UUID más reciente es el de la conversación actual
            latest_uuid = uuid_candidates[0]["values"][0] if uuid_candidates else None
            
            return {
                "success": True,
                "uuid": latest_uuid,
                "candidates": uuid_candidates,
                "db_path": str(DB_PATH),
            }
        else:
            return {
                "success": False,
                "error": "No se encontró columna con UUID",
                "tables": tables,
            }
            
    except sqlite3.OperationalError as e:
        if "database is locked" in str(e):
            return {
                "success": False,
                "error": "Database is locked by Brave browser",
                "solution": "Close Brave or wait for it to release the lock",
                "db_path": str(DB_PATH),
            }
        raise
    except Exception as e:
        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
        }


if __name__ == "__main__":
    print("🔍 Leyendo UUID de Brave AI Chat desde SQLite\n")
    print("=" * 70)
    print(f"📁 DB Path: {DB_PATH}")
    print()
    
    result = read_uuid_from_db()
    
    print("\n" + "=" * 70)
    print("📊 RESULTADO:")
    if result["success"]:
        print(f"✅ UUID actual: {result['uuid']}")
        print(f"📁 DB: {result['db_path']}")
    else:
        print(f"❌ Error: {result['error']}")
        if "solution" in result:
            print(f"💡 Solución: {result['solution']}")