# Smart Energy Meter — IEEE 2030.5 (Xcel / Itron)

A local, read-only Home Assistant custom integration for smart electric meters.
Start with Xcel Energy's Itron meters; use standard IEEE 2030.5 metering links
where a device advertises them. No MQTT bridge, extra container, or cloud poller.

**Initial release:** tested with a simulated mutual-TLS meter and Home Assistant
2026.10.0. Physical-meter and other-provider compatibility must still be validated.
This implements a metering subset, not the entire IEEE 2030.5 specification.

## Install

Requires Home Assistant **2026.10.0 or later**.

1. In HACS, add `https://github.com/keatontaylor/ha-ieee2030-5` as a custom repository,
   category **Integration**, then install **Smart Energy Meter (Xcel / Itron)**.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, then search for
   **Smart Energy Meter**. An mDNS-discovered meter may already appear there.
4. Enter the meter address, HTTPS port (usually 8081), and connection profile.
5. Upload the client certificate and private key registered with your utility,
   in PEM format. Supply a key passphrase if encrypted. Optionally upload a CA.
6. The wizard verifies TLS, meter identity and supported registers before saving.

Manual installation: download `ieee2030_5.zip` from
[Releases](https://github.com/keatontaylor/ha-ieee2030-5/releases), extract it to
`/config/custom_components/ieee2030_5/`, and restart HA. Do not copy repository
root files to `custom_components`. The ZIP contains the component files directly.
HACS custom repository installation works before inclusion in its default catalog.

## Credentials and TLS

Obtain and register a certificate/key pair using your utility's meter-access
process. This integration does not enroll a certificate or modify the meter.
Certificates, private keys and optional CA files are stored inside HA's persistent
`/config/.storage/ieee2030_5/`, with private file permissions. Include them in
protected HA backups. The key passphrase, if provided, is stored in HA's config
entry; Home Assistant administrators can access it. Files are never committed to
this repo, exposed through diagnostics, or included in log messages.

**Itron / Xcel:** uses TLS 1.2 and `ECDHE-ECDSA-AES128-CCM8`. The required OpenSSL
security level is lowered **only on this meter's SSL context**. It also permits
an initial connection to older servers that omit secure-renegotiation support,
while disabling subsequent TLS renegotiation. These compatibility settings carry
legacy TLS risks and are confined to the explicitly selected Itron profile. No global
`OPENSSL_CONF`, patched libraries, or shared HA client settings. Without a CA,
server certificate verification is disabled, matching the existing Itron bridges;
only use this mode on a trusted network. Upload a CA to enable server certificate
and hostname verification. The entered hostname/IP must then match its certificate.

**Standard:** uses normal verified TLS; a CA upload is required. No legacy cipher
or security-level override. A provider using different certificates or resources
may require its own device profile; this is not universal compatibility.

Use **Reconfigure** on the integration to update the address, instantaneous polling interval,
or replace credentials. Omit uploads to retain existing credentials. Upload the
full certificate/key/optional-CA set to replace them. New files are validated
before old files are removed. A different meter identity is rejected. Removing
the integration removes its stored credential files.

### Polling intervals

Registers have separate schedules, with these defaults:

| Reading | Interval |
| --- | --- |
| Instantaneous demand / real, apparent and reactive power | 5 seconds |
| Power factor, voltage, current and frequency | 30 seconds |
| Cumulative real, apparent and reactive energy | 60 seconds |
| TOU energy counters (including tier 0) | 5 minutes |
| Maximum demand | 15 minutes |

The wizard's **Instantaneous polling interval** controls the fastest readings.
Choosing a longer interval also slows any category that would otherwise run
faster. Existing installations retain their saved interval: use **Reconfigure**
to select five seconds if desired; certificate uploads are not required.

Each refresh reads one due resource, giving instantaneous measurements priority
and preserving cached slower values. Counters are staggered rather than fetched
in one batch. Requests are serialized with a half-second pause after each
response, including during discovery. Shared discovery resources are fetched
once per discovery, and setup reuses the discovered initial readings.

Intervals are targets measured after a request completes, not guarantees: slow
responses and a meter with many instantaneous registers can extend them. Slow
counter requests have a timeout limited by the next demand read. A failed or
empty register becomes unavailable and backs off independently, from at least
30 seconds up to 15 minutes (or the configured interval if longer). Other
readings continue updating. Authentication failures still trigger reauthentication.
Entity attributes and downloaded diagnostics include the effective interval.

Successful requests do not prove that the meter supplied a fresh measurement.
Home Assistant may keep its displayed update timestamp unchanged when a value
and its attributes are unchanged, even though polling continues.

mDNS uses `_smartenergy._tcp.local.` through HA's existing discovery service.
Manual addressing works when multicast discovery cannot cross your VLANs.
Discovery does not scan the subnet or configure devices without confirmation.

## Sensors and discovery

The client starts at `/dcap`, follows advertised self-device, device-information,
usage-point, meter-reading, reading-set and reading links, and interprets
`ReadingType` unit, power-of-ten multiplier, accumulation and direction metadata.
List pagination is supported. Requests have size/time/resource limits; discovered
links cannot forward client credentials to another server, and redirects are not
followed. XML DTDs and entities are rejected.

The Itron profile skips historical reading-set archives and probes the known
firmware-map endpoints for current readings from
`xcel_itron2mqtt`: instantaneous demand, imported/exported energy, apparent and
reactive energy, TOU registers 0–3, maximum demand, and power factor per phase.
Only endpoints that return a valid reading create sensors; missing endpoints and
empty Reading resources (such as an unused power-factor phase) are skipped. An
empty Reading returned later marks its sensor unavailable. Where advertised metadata exists, it takes precedence over the fallback.

TOU register numbers do **not** universally mean on-/off-peak. Rename entities in
HA to match your utility's documented tariff after confirming the mapping. No
price, billing forecast, demand-response, or DER-control functionality is included.

Imported/exported cumulative Wh sensors can be selected in HA's Energy dashboard.
Apparent/reactive energy remain VAh/varh; they are not mislabeled as real energy.
Net energy metadata uses a total state class so decreases remain meaningful.
Unavailable or missing readings are not replaced with zero. Entity IDs use the
meter LFDI and resource path, so an IP change preserves history.

## Automated maintenance

- No additional integration runtime dependencies: aiohttp and the schema/UI APIs
  are supplied by Home Assistant. The isolated development environment uses `uv`.
- Every pull request runs lint, actual CCM8 mutual-TLS tests, HA wizard and lifecycle
  tests, packaging checks, minimum-HA tests, and Home Assistant's `hassfest`.
- Renovate updates the pinned test environment, lockfile and Action digests.
  Patch/minor/digest PRs auto-merge after required checks; majors require review.
  New versions must be at least three days old. HA and its harness update together.
- Every passing merge to main publishes a GitHub tag and HACS install ZIP. Version
  format is `major.minor.<CI run number>`; major/minor come from the source manifest.
  The package manifest contains the full release version. Scheduled/manual checks
  do not publish releases. Test and release jobs share one workflow so a GitHub
  token-created tag does not need to trigger another workflow.
- Weekly CI reruns expose changes in runner/OpenSSL behavior. Beta HA testing is
  not currently automated; the supported stable environment and minimum baseline
  are both checked.

Enable the Renovate GitHub App for this repository. Repository auto-merge and
branch protection must require **Required checks**, **Minimum Home Assistant**, and
**Home Assistant validation**. The GitHub Actions release job only needs the
repository's built-in token; no meter certificates or extra release tokens.

Run locally:

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Testing boundaries

The fake HTTPS meter generates an ECDSA CA, server and client certificates,
requires a valid client certificate and negotiates the actual CCM8 cipher. Tests
cover standard resource discovery, pagination, firmware fallback, signed/scaled
readings, malformed XML, cross-origin links, certificate rotation, setup failure,
identity mismatch, sensor availability, cleanup and ZIP structure. Controlled-clock
tests cover separate register cadences, automatic HA polling, demand priority,
request pacing, cached counter values and per-register failure backoff.

Mock fixtures do not prove a physical meter's firmware, tariff labels, certificate
enrollment, or all HA/OpenSSL versions. Keep your existing bridge until you compare
live readings, especially imported/exported totals and TOU registers. File an issue
with sanitized XML and firmware information if a meter advertises unsupported
metadata; never attach certificates, private keys, account IDs, or meter LFDIs.

## Attribution

Apache-2.0. Itron endpoint locations are adapted from
[zaknye/xcel_itron2mqtt](https://github.com/zaknye/xcel_itron2mqtt).
Discovery design also references
[user00265/itron-go-mqtt](https://github.com/user00265/itron-go-mqtt).
See [NOTICE](NOTICE) for revisions and adaptation details. IEEE constants follow
its published 2030.5-2023 schema; no claim of certification is made.
