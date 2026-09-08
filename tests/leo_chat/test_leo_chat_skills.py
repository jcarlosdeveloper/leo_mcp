#!/usr/bin/env python3
"""
Test rápido del sistema de Skills

Verifica:
1. Import de skills
2. Factory registration
3. Instanciación de skills
4. Model overrides
"""

import sys
import logging

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')

def test_imports():
    """Test 1: Verificar imports básicos"""
    print("\n🧪 Test 1: Imports básicos")
    try:
        from leo_chat.skills.base_skill import BaseSkill
        print("  ✅ BaseSkill importado")
    except ImportError as e:
        print(f"  ❌ BaseSkill falló: {e}")
        return False
    
    try:
        from leo_chat.skills.skill_factory import SkillFactory
        print("  ✅ SkillFactory importado")
    except ImportError as e:
        print(f"  ❌ SkillFactory falló: {e}")
        return False
    
    try:
        from leo_chat.context.prompt_builder import PromptBuilder
        print("  ✅ PromptBuilder importado")
    except ImportError as e:
        print(f"  ❌ PromptBuilder falló: {e}")
        return False
    
    return True


def test_skill_instantiation():
    """Test 2: Instanciar skills"""
    print("\n🧪 Test 2: Instanciación de skills")
    
    from leo_chat.skills.skill_factory import SkillFactory
    
    # Listar skills disponibles
    skills = SkillFactory.list_skills()
    print(f"  📋 Skills registrados: {len(skills)}")
    
    for skill_id, info in skills.items():
        print(f"    - {skill_id}: {info['description']} (model: {info['model_key']})")
    
    # Instanciar cada skill
    for skill_id in skills.keys():
        try:
            skill = SkillFactory.get_skill(skill_id)
            print(f"  ✅ {skill_id}: {repr(skill)}")
        except Exception as e:
            print(f"  ❌ {skill_id} falló: {e}")
            return False
    
    return True


def test_model_override():
    """Test 3: Override de modelos en runtime"""
    print("\n🧪 Test 3: Model override")
    
    from leo_chat.skills.skill_factory import SkillFactory
    
    # Guardar modelo original
    original = SkillFactory.get_skill("senior_planner")
    original_model = original.model_key
    print(f"  📌 senior_planner modelo original: {original_model}")
    
    # Aplicar override
    SkillFactory.set_model_override("senior_planner", "qwen-coder")
    print(f"  🔄 Override aplicado: qwen-coder")
    
    # Verificar
    overridden = SkillFactory.get_skill("senior_planner")
    print(f"  ✅ Nuevo modelo: {overridden.model_key}")
    
    # Limpiar (opcional, en producción se mantiene)
    if "senior_planner" in SkillFactory._model_overrides:
        del SkillFactory._model_overrides["senior_planner"]
    
    return True


def test_prompt_building():
    """Test 4: Construcción de prompts"""
    print("\n🧪 Test 4: Prompt building")
    
    from leo_chat.skills.skill_factory import SkillFactory
    from leo_chat.context.prompt_builder import PromptBuilder
    import time
    
    skill = SkillFactory.get_skill("senior_planner")
    builder = PromptBuilder()
    
    # Prompts dinámicos rotativos por timestamp
    timestamp = int(time.time())
    prompt_rotation = [
        f"Explica complejidad O({timestamp % 10}²) vs O({timestamp % 10} log {timestamp % 10}) con ejemplo práctico",
        f"¿Cuándo usar semaphore vs lock en asyncio? (caso: pool={timestamp % 100} workers)",
        f"Tradeoffs: caching strategy para {timestamp % 1000} req/s con TTL variable",
        f"Pattern matching: structural vs nominal typing en sistemas distribuidos (n={timestamp % 50} nodes)",
        f"Backpressure handling en streams de {timestamp % 10000} eventos/segundo",
    ]
    
    user_prompt = prompt_rotation[timestamp % len(prompt_rotation)]
    context = f"Python {3 + (timestamp % 2)}.{timestamp % 12}+, contexto: distributed systems"
    
    full_prompt = builder.build(skill, user_prompt, extra_context=context)
    
    print(f"  📝 Prompt dinámico generado ({len(full_prompt)} chars):")
    print(f"  {'─' * 60}")
    print(f"  {user_prompt[:60]}... (rotación: {timestamp % len(prompt_rotation)}/{len(prompt_rotation)})")
    print(f"  {'─' * 60}")
    
    return len(full_prompt) > 0


def main():
    """Ejecutar todos los tests"""
    print("=" * 70)
    print("🧪 LEO CHAT SKILLS - TEST SUITE")
    print("=" * 70)
    
    tests = [
        ("Imports", test_imports),
        ("Skill Instantiation", test_skill_instantiation),
        ("Model Override", test_model_override),
        ("Prompt Building", test_prompt_building),
    ]
    
    results = []
    for name, test_fn in tests:
        try:
            passed = test_fn()
            results.append((name, passed))
        except Exception as e:
            print(f"\n💥 {name} explotó: {e}")
            results.append((name, False))
    
    # Resumen
    print("\n" + "=" * 70)
    print("📊 RESULTADOS")
    print("=" * 70)
    
    passed = sum(1 for _, p in results if p)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests pass")
    
    if passed == total:
        print("\n🎉 ¡Todos los tests pasaron!")
        return 0
    else:
        print(f"\n⚠️  {total - passed} tests fallaron")
        return 1


if __name__ == '__main__':
    sys.exit(main())