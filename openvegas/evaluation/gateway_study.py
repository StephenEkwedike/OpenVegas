"""Explicitly funded, headless 20-agent study; no real wagers or provider keys."""
import argparse
from pathlib import Path

from openvegas.integrations.credentials import Credentials, backend_url

from . import host_study
from .gateway_agent import GatewayAgent, GatewayStudy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--claude-model', required=True)
    parser.add_argument('--codex-model', required=True)
    parser.add_argument('--per-agent-limit-v', required=True)
    parser.add_argument('--total-limit-v', required=True)
    parser.add_argument('--confirm-inference-spend', action='store_true')
    args = parser.parse_args()
    if not args.confirm_inference_spend:
        parser.error('Explicit inference spending consent required; simulated chips do not authorize provider spending.')
    # Fresh directories prevent reruns from silently resetting funding limits.
    if args.home.exists() or args.output.exists():
        parser.error('Use new study directories; funding cannot be reset by rerunning.')
    backend = backend_url()
    device = Credentials(backend).load()
    study = GatewayStudy(backend, device, total_limit_v=args.total_limit_v,
                         per_agent_limit_v=args.per_agent_limit_v, max_agents=20)
    models = {'claude': args.claude_model, 'codex': args.codex_model}
    host_study.prepare(args.home.resolve(), args.output.resolve(), models=models, inference={
        'source': 'admin_sponsored_openvegas_gateway', 'total_ceiling_v': str(study.total),
        'per_agent_ceiling_v': str(study.per_agent), 'max_concurrency': 2,
        'wager_unit': 'simulated_V', 'real_wagers': False, 'chip_grants_fund_inference': False})
    try:
        for host in ('claude', 'codex'):
            host_study.run_host(args.home.resolve(), host,
                adapter=GatewayAgent(study, host, models[host]), readiness={
                    'host': host, 'ready': True, 'subscription_ready': False,
                    'auth_method': 'scoped_openvegas_gateway', 'reason': None})
    finally:
        study.close()
    print(host_study.verify(args.output.resolve()))


if __name__ == '__main__':
    main()
