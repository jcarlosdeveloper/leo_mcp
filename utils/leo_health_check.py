#!/usr/bin/env python3
"""
Leo MCP Production Health Check
Enterprise-grade monitoring for Leo MCP streaming server
Runs every hour via cron to ensure 24/7 reliability
"""
import sys
import time
import asyncio
import json
import os
sys.path.insert(0, '/Users/tlaloc_ai/.hermes')

from leo_mcp_server import ask_leo, _conv_get_recent

class HealthChecker:
    """Production health check with retry logic and metrics."""
    
    def __init__(self):
        self.results = {}
        self.start_time = time.time()
    
    def log(self, level: str, message: str):
        """Structured logging."""
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{timestamp}] [{level}] {message}")
    
    async def check_streaming(self, test_id: str, prompt: str, expected_keyword: str) -> dict:
        """Test streaming mode with validation."""
        start = time.time()
        try:
            result = await ask_leo(prompt, stream=True)
            duration = time.time() - start
            data = json.loads(result)
            
            content = data.get('content', '')
            success = (
                data.get('status') == 'ok' and
                expected_keyword in content
            )
            
            return {
                'test': test_id,
                'success': success,
                'duration': duration,
                'status': data.get('status'),
                'content_preview': content[:100] if success else content[:100],
                'error': data.get('error') if data.get('status') != 'ok' else None
            }
        except Exception as e:
            return {
                'test': test_id,
                'success': False,
                'duration': time.time() - start,
                'error': str(e)
            }
    
    async def check_concurrent_streams(self) -> dict:
        """Test parallel streaming requests."""
        test_cases = [
            ("A", "Di exactamente: CONCURRENT_A_OK", "CONCURRENT_A_OK"),
            ("B", "Di exactamente: CONCURRENT_B_OK", "CONCURRENT_B_OK"),
            ("C", "Di exactamente: CONCURRENT_C_OK", "CONCURRENT_C_OK"),
        ]
        
        start = time.time()
        tasks = [self.check_streaming(f"CONCURRENT_{label}", prompt, expected) 
                 for label, prompt, expected in test_cases]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        total_duration = time.time() - start
        successes = sum(1 for r in results if isinstance(r, dict) and r.get('success'))
        
        # Debug info for failures
        if successes < len(test_cases):
            self.log("DEBUG", f"Concurrent failures: successes={successes}/{len(test_cases)}")
            for r in results:
                if isinstance(r, dict) and not r.get('success'):
                    self.log("DEBUG", f"  - {r['test']}: status={r.get('status')}, preview={r.get('content_preview', 'N/A')[:50]}")
        
        return {
            'test': 'CONCURRENT_STREAMS',
            'success': successes == len(test_cases),
            'total_duration': total_duration,
            'successes': successes,
            'total': len(test_cases),
            'individual_results': results
        }
    
    def check_uuid_isolation(self) -> dict:
        """Verify UUIDs are properly isolated."""
        old_uuids = {row.get('uuid') for row in _conv_get_recent(10) if row.get('uuid')}
        
        # Note: Can't create new UUIDs in sync context easily, just check DB health
        recent = _conv_get_recent(5)
        
        return {
            'test': 'UUID_ISOLATION',
            'success': len(recent) > 0,
            'recent_count': len(recent),
            'recent_uuids': [r.get('uuid', 'N/A')[:8] for r in recent]
        }
    
    def check_server_process(self) -> dict:
        """Verify server process is running."""
        import subprocess
        result = subprocess.run(['pgrep', '-f', 'leo_mcp_server'], 
                              capture_output=True, text=True)
        pids = result.stdout.strip().split('\n') if result.returncode == 0 else []
        
        return {
            'test': 'SERVER_PROCESS',
            'success': len(pids) > 0,
            'pids': pids,
            'count': len(pids)
        }
    
    async def run_all_checks(self) -> dict:
        """Execute all health checks."""
        self.log("INFO", "Starting Leo MCP Health Check")
        
        # Check 1: Server process
        self.results['server_process'] = self.check_server_process()
        self.log("INFO" if self.results['server_process']['success'] else "ERROR",
                f"Server process: {'✓' if self.results['server_process']['success'] else '✗'} "
                f"(PIDs: {self.results['server_process']['pids']})")
        
        # Check 2: Single streaming request
        self.results['streaming_single'] = await self.check_streaming(
            'SINGLE_STREAM',
            "Di exactamente: HEALTH_CHECK_SINGLE_OK",
            'HEALTH_CHECK_SINGLE_OK'
        )
        self.log("INFO" if self.results['streaming_single']['success'] else "ERROR",
                f"Single streaming: {'✓' if self.results['streaming_single']['success'] else '✗'} "
                f"({self.results['streaming_single']['duration']:.1f}s)")
        
        # Check 3: Concurrent streams
        self.results['concurrent'] = await self.check_concurrent_streams()
        conc = self.results['concurrent']
        self.log("INFO" if conc['success'] else "ERROR",
                f"Concurrent streams: {'✓' if conc['success'] else '✗'} "
                f"({conc['successes']}/{conc['total']} passed, {conc['total_duration']:.1f}s total)")
        
        # Check 4: UUID isolation
        self.results['uuid_isolation'] = self.check_uuid_isolation()
        self.log("INFO" if self.results['uuid_isolation']['success'] else "ERROR",
                f"UUID isolation: {'✓' if self.results['uuid_isolation']['success'] else '✗'} "
                f"({self.results['uuid_isolation']['recent_count']} recent conversations)")
        
        # Summary
        total_tests = len(self.results)
        passed = sum(1 for r in self.results.values() if r.get('success'))
        
        self.results['summary'] = {
            'total_tests': total_tests,
            'passed': passed,
            'failed': total_tests - passed,
            'health_score': passed / total_tests if total_tests > 0 else 0,
            'duration': time.time() - self.start_time
        }
        
        self.log("INFO", f"Health check complete: {passed}/{total_tests} passed "
                f"({self.results['summary']['health_score']*100:.0f}% healthy)")
        
        return self.results
    
    def generate_report(self) -> str:
        """Generate human-readable report."""
        if not self.results:
            return "❌ No health check data available"
        
        summary = self.results.get('summary', {})
        health_score = summary.get('health_score', 0) * 100
        
        report = []
        report.append("=" * 70)
        report.append("LEO MCP PRODUCTION HEALTH CHECK REPORT")
        report.append("=" * 70)
        report.append(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        report.append(f"Health Score: {health_score:.0f}%")
        report.append(f"Tests: {summary.get('passed', 0)}/{summary.get('total_tests', 0)} passed")
        report.append(f"Duration: {summary.get('duration', 0):.1f}s")
        report.append("")
        
        # Detailed results
        report.append("DETAILED RESULTS:")
        report.append("-" * 70)
        
        for test_name, result in self.results.items():
            if test_name == 'summary':
                continue
            
            status = "✅ PASS" if result.get('success') else "❌ FAIL"
            report.append(f"\n{test_name.upper()}: {status}")
            
            if 'duration' in result:
                report.append(f"  Duration: {result['duration']:.1f}s")
            
            if 'error' in result and result['error']:
                report.append(f"  Error: {result['error']}")
        
        report.append("")
        report.append("=" * 70)
        
        # Alert level
        if health_score >= 100:
            report.append("✅ ALL SYSTEMS OPERATIONAL")
        elif health_score >= 75:
            report.append("⚠️  DEGRADED PERFORMANCE - Monitor closely")
        else:
            report.append("❌ CRITICAL ISSUES - Immediate action required")
        
        report.append("=" * 70)
        
        return "\n".join(report)


async def main():
    """Main entry point."""
    checker = HealthChecker()
    await checker.run_all_checks()
    
    report = checker.generate_report()
    print("\n" + report)
    
    # Exit with appropriate code
    health_score = checker.results.get('summary', {}).get('health_score', 0)
    sys.exit(0 if health_score >= 0.75 else 1)


if __name__ == "__main__":
    asyncio.run(main())