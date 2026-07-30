__version__ = "0.1.0"

# Use the operating system's certificate store instead of certifi's bundle.
#
# Several boards (ford.com, careers.microsoft.com) fail certifi verification
# with CERTIFICATE_VERIFY_FAILED on a machine behind a TLS-inspecting proxy,
# because the proxy's root lives in the OS store and never in certifi. The
# failure is indistinguishable from a dead board, so it quietly misfiled
# working boards as needing a browser.
#
# This raises the number of boards that verify. It does NOT lower the bar:
# verification stays on, and a genuinely bad certificate still fails. Never
# replace this with verify=False.
#
# It lives in __init__ because every entry point (cli, tools/verify,
# tools/discover, mcp_server) imports the package, and an SSL context created
# before injection would keep the old behavior. A missing truststore is not
# fatal, it just means the previous certifi behavior applies.
try:
    import truststore as _truststore
except ImportError:  # pragma: no cover
    pass
else:
    _truststore.inject_into_ssl()
