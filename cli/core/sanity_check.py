import argparse

from cli.core import env, load_env
from cli.core.sync import run_sanity_check


def run(args: argparse.Namespace) -> None:
    load_env()
    droplet_ip = env("DROPLET_IP")
    run_sanity_check(droplet_ip)
