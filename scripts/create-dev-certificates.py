"""Create a short-lived private test CA and server/client certificates on Linux."""
import argparse
import ipaddress
import os
from pathlib import Path
import subprocess


def generate(output, server_ip):
    server_ip = str(ipaddress.IPv4Address(server_ip))
    previous_mask = os.umask(0o077)
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)

        def openssl(*args):
            subprocess.run(["openssl", *args], cwd=output, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        key_options = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"]
        openssl("req", "-x509", *key_options, "-keyout", "ca.key", "-out", "ca.crt",
                "-days", "7", "-subj", "/CN=Forge development CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        for name, purpose in (("server", "serverAuth"), ("client", "clientAuth")):
            openssl("req", "-new", *key_options, "-keyout", f"{name}.key",
                    "-out", f"{name}.csr", "-subj", f"/CN=forge-dev-{name}")
            extensions = ("basicConstraints=critical,CA:FALSE\n"
                          "keyUsage=critical,digitalSignature\n"
                          f"extendedKeyUsage={purpose}\n")
            if name == "server":
                extensions += f"subjectAltName=IP:{server_ip}\n"
            (output / f"{name}.ext").write_text(extensions)
            openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt",
                    "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.crt",
                    "-days", "7", "-extfile", f"{name}.ext")
    finally:
        os.umask(previous_mask)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the repository")
    parser.add_argument("--server-ip", required=True, help="coordinator IPv4 address for its certificate SAN")
    args = parser.parse_args()
    generate(args.output.resolve(), args.server_ip)
    print("Created seven-day development credentials. Keep ca.key private; distribute only each peer's credentials.")
