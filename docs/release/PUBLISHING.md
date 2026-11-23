# Publishing tuneio

The `Publish tuneio` workflow is started manually from `main`. It downloads the wheel and source archive already attached to the selected GitHub release, checks their identity and runtime source against the tag, validates metadata, and runs the installed-wheel HTTP smoke. Only verified artifacts reach the upload job. The destination is selected for each run.

## Account setup

Create accounts at https://test.pypi.org/account/register/ and https://pypi.org/account/register/. Verify your email and complete the account's two-factor authentication setup. These registries have separate accounts and publisher settings.

In this GitHub repository, open **Settings > Environments**. Create the environments `testpypi` and `pypi`. Where available, add a required reviewer to `pypi` to review the production upload.

For the first release, open **Publishing** in your TestPyPI account settings and add a pending GitHub publisher with these fields.

| Field | TestPyPI value | PyPI value |
| --- | --- | --- |
| Project name | `tuneio` | `tuneio` |
| Owner | `Vedikgannoji` | `Vedikgannoji` |
| Repository name | `API-Performance-Auto-Tuner` | `API-Performance-Auto-Tuner` |
| Workflow filename | `publish.yml` | `publish.yml` |
| Environment name | `testpypi` | `pypi` |

Create the corresponding pending publisher in your PyPI account after the rehearsal. If you already own the project, use the project's **Manage > Publishing** settings. Registry ownership and name availability are determined by each registry. Trusted Publishing authenticates the workflow through GitHub OIDC.

## Rehearse the upload

Commit and push the publishing changes to `main`. Create tag `v2.1.0b3` on that commit and attach the new 2.1.0b3 wheel and source archive to its GitHub prerelease. The new version includes the MIT license. Use a new version if an earlier artifact with that version has already been uploaded to the destination.

In GitHub **Actions > Publish tuneio > Run workflow**, choose branch `main`, tag `v2.1.0b3`, and registry `testpypi`. Wait for both jobs to finish. Open https://test.pypi.org/project/tuneio/2.1.0b3/ and inspect the version and files.

Run the following in a new Command Prompt to verify the TestPyPI package in a separate environment. Dependencies are installed from PyPI, followed by the exact tuneio wheel from TestPyPI. A fresh environment directory is required.

```bat
cd /d E:\API-Performance-Auto-Tuner
python -m venv tmp\tuneio-testpypi-check
tmp\tuneio-testpypi-check\Scripts\activate
python -m pip install "fastapi>=0.104.1,<1" "uvicorn>=0.24,<1" "pydantic>=2.5,<3" "httpx>=0.27,<1"
python -m pip install --index-url https://test.pypi.org/simple/ --no-deps --only-binary=:all: "tuneio==2.1.0b3"
cd /d %TEMP%
tuneio --version
tuneio menu --json
tuneio --help
deactivate
```

## Publish to PyPI

Finish the PyPI publisher setup with environment `pypi`. Run the same GitHub workflow from `main` with the same tag and destination `pypi`. Approve the environment deployment if configured. This publishes the GitHub release distributions validated in that run.

Verify https://pypi.org/project/tuneio/2.1.0b3/ and install the exact beta in a fresh environment. All users can then choose one installer.

```sh
python -m pip install "tuneio==2.1.0b3"
pipx install "tuneio==2.1.0b3"
uv tool install "tuneio==2.1.0b3"
```

Run `tuneio --version` and `tuneio` after installing. The Python distribution currently supports Python 3.11 through 3.13. Follow the terminal guide for API setup and traffic capture.

## Current release decisions

The owner selected the MIT license. The license file and distribution metadata are included in version 2.1.0b3. The separate candidate checker tracks qualification, soak and reviewer evidence. Its status should accompany beta readiness claims.
