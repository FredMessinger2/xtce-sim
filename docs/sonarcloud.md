# SonarQube Cloud setup

Static analysis and coverage tracking run on [SonarQube Cloud](https://sonarcloud.io)
(the hosted service formerly called SonarCloud — free for public repositories).

The config is already committed:

- [`sonar-project.properties`](../sonar-project.properties) — analysis scope + coverage report path.
- [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) — runs tests on Python
  3.11/3.12/3.13, produces `coverage.xml`, and runs the Sonar scan.

## One-time activation (after the repo is on GitHub)

1. **Import the repo** at https://sonarcloud.io (sign in with GitHub).
2. **Fill in the keys.** Copy the Organization Key and Project Key that SonarCloud
   assigns into `sonar-project.properties` (replace the `REPLACE_WITH_…` placeholders).
3. **Add the token.** In SonarCloud generate an analysis token, then add it to the
   GitHub repo as a secret named `SONAR_TOKEN`
   (Settings → Secrets and variables → Actions).
4. **Disable Automatic Analysis.** In the SonarCloud project,
   Administration → Analysis Method → turn *off* Automatic Analysis. This is
   required so the CI scan (which uploads coverage) is used instead — the two
   conflict.
5. **Align the Quality Gate.** Set the gate's coverage condition to 90% to match
   `fail_under` in `pyproject.toml`, and enable PR decoration.

## Notes

- Coverage uses coverage.py's `relative_files = true` (in `pyproject.toml`) so the
  paths in `coverage.xml` resolve on the CI runner.
- The Sonar job skips automatically on fork pull requests, where `SONAR_TOKEN`
  isn't available.
- Until activation, the `test` job still runs (lint + tests + the 90% coverage
  gate); only the `sonar` scan step is inert.
