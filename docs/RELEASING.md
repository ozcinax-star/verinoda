# Releasing Verinoda

A release is one tag. `.github/workflows/release.yml` builds the sdist and the wheel once, checks them
(`twine check --strict`, a clean install that scans the example project and answers a query, the npm
wrapper's version), then publishes the same files to PyPI, attaches them to a GitHub release, and publishes
the npm wrapper (`packaging/npm`) of the same version.

No password or PyPI token is stored anywhere: PyPI accepts the workflow itself through Trusted Publishing
(OpenID Connect). npm needs one token, kept as a repository secret.

## One-time setup (the maintainer)

1. **PyPI.** Sign in at https://pypi.org, then *Your account -> Publishing -> Add a new pending publisher*:
   PyPI project name `verinoda`, owner `ozcinax-star`, repository `verinoda`, workflow `release.yml`,
   environment `pypi`. Do the same at https://test.pypi.org with environment `testpypi` (for rehearsals).
2. **GitHub environments (optional).** GitHub creates `pypi` and `testpypi` the first time the workflow uses
   them. Create them yourself (*Settings -> Environments*) only to add protection: yourself as a required
   reviewer on `pypi` makes every PyPI upload wait for your click.
3. **npm.** Sign in at https://www.npmjs.com, create a *granular access token* with read and write access
   to packages (it can be limited to `verinoda` after the first publish), and store it as the repository
   secret `NPM_TOKEN` (*Settings -> Secrets and variables -> Actions*). The package is published with
   provenance, which links it to this workflow run. Without the secret the release still goes to PyPI and
   GitHub; the npm step only warns.

## Rehearsal (TestPyPI)

*Actions -> release -> Run workflow* on `main` with `target = testpypi`. Then, in a fresh environment:

```bash
uv tool run --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ \
  --index-strategy unsafe-best-match --from verinoda verinoda --version
```

TestPyPI refuses the same version twice; bump a rehearsal version (for example `0.1.0rc1`) if you repeat it.

## A release

1. Set the version in two places: `version` in `pyproject.toml` (PEP 440: `0.1.0`, `0.2.0rc1`) and
   `version` in `packaging/npm/package.json` (the npm spelling of the same version: `0.1.0`,
   `0.2.0-rc.1`; the workflow checks that they match, and the wrapper maps one to the other).
2. Update the README status table and `docs/UPGRADING.md`; commit on `main`; wait for CI.
3. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`.
4. The workflow publishes to PyPI (after the `pypi` environment's approval, if you set one), creates the
   GitHub release, and publishes to npm.
5. Check: `uvx --from verinoda==0.1.0 verinoda --version`, `npx -y verinoda@0.1.0 --version`.

A version on PyPI can never be uploaded again, even after it is deleted: fix a bad release with a new
version (`0.1.1`), not by re-tagging.

## Other channels

- **uv / pipx / pip**: work from PyPI with no extra step.
- **npx**: the wrapper runs the PyPI release through `uvx`, `pipx run`, or a private virtual environment
  made once per version with a Python 3.10+ from PATH. MCP clients can start the server with
  `npx -y verinoda mcp serve`.
- **install.sh / the GitHub archive**: install the development version (`main`) or any ref
  (`VERINODA_REF`).
- Not provided: a PowerShell `irm | iex` installer (Microsoft Defender flagged that pattern for this project),
  Homebrew, winget or Docker packages. uv is itself on winget and Homebrew, and one `uv tool install` line
  covers all three systems.
