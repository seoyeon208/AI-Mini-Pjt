"""실제 stdio 프로토콜에서 실행 구간·응답 대응·부분 실패를 확인하는 테스트 서버."""
import asyncio
import time

from fastmcp import FastMCP

mcp = FastMCP('concurrency-test')
DELAYS = {'auth-service': 0.20, 'payments-service': 0.06, 'gateway-service': 0.12,
          'cache-service': 0.08}


@mcp.tool()
async def get_monitoring_status(system_name: str) -> dict:
    started = time.monotonic()
    await asyncio.sleep(DELAYS[system_name])
    if system_name == 'cache-service':
        raise ValueError('controlled monitoring failure')
    return {'system_name': system_name, 'status': 'healthy',
            'cpu_percent': {'auth-service': 20, 'payments-service': 30, 'gateway-service': 40}[system_name],
            'memory_percent': 50, 'alerts': [], 'last_checked': '2026-10-21T00:00:00',
            'started': started, 'finished': time.monotonic()}


if __name__ == '__main__':
    mcp.run(show_banner=False)
