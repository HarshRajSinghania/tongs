#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'usage: %s --output-dir DIR [--accepted-dir DIR --payload-manifest FILE]\n' "$0" >&2
    printf '       %s --output-dir DIR --accepted-dir DIR --payload-manifest FILE --local-builder IMAGE\n' "$0" >&2
}
output_dir=""
accepted_input=""
payload_manifest=""
local_builder=""
while (($#)); do
    case "$1" in
        --output-dir) output_dir=$2; shift 2 ;;
        --accepted-dir) accepted_input=$2; shift 2 ;;
        --payload-manifest) payload_manifest=$2; shift 2 ;;
        --local-builder) local_builder=$2; shift 2 ;;
        *) usage; exit 2 ;;
    esac
done
[[ -n "$output_dir" ]] || { usage; exit 2; }
if [[ -n "$accepted_input" || -n "$payload_manifest" ]]; then
    [[ -d "$accepted_input" && -f "$payload_manifest" ]] || { usage; exit 2; }
fi
if [[ -n "$local_builder" ]]; then
    # Local mode: a developer's own Podman loop with a builder image built
    # from packaging/rpm/builder/Containerfile.  It never runs in a workflow.
    [[ -z ${GITHUB_ACTIONS+set} && -z ${RUNNER_ENVIRONMENT+set} ]] || {
        printf 'the local builder mode is refused when GITHUB_ACTIONS or RUNNER_ENVIRONMENT is set\n' >&2
        exit 2
    }
    [[ "$local_builder" =~ ^localhost/[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_.-]+)?$ ]] || {
        printf 'the local builder must be a localhost/ image reference: %s\n' "$local_builder" >&2
        exit 2
    }
    [[ -n "$accepted_input" ]] || {
        printf 'the local builder mode needs --accepted-dir and --payload-manifest\n' >&2
        exit 2
    }
else
    [[ ${GITHUB_ACTIONS:-false} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || {
        printf 'this RPM harness may run only on a GitHub-hosted disposable runner\n' >&2
        exit 2
    }
    command -v gh >/dev/null || { printf 'gh is required\n' >&2; exit 2; }
fi
command -v podman >/dev/null || { printf 'podman is required\n' >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
source_sha=$(git -C "$repo_root" rev-parse HEAD)
requested_sha=${TONGS_HEAD_SHA:-}
[[ -n "$requested_sha" && "$source_sha" == "$requested_sha" ]] || {
    printf 'source revision %s does not match requested head %s\n' "$source_sha" "$requested_sha" >&2
    exit 1
}
mkdir -p -- "$output_dir"
output_dir=$(cd -- "$output_dir" && pwd)
if find "$output_dir" -mindepth 1 -print -quit | grep -q .; then
    printf 'output directory must be empty: %s\n' "$output_dir" >&2
    exit 2
fi
case "$output_dir/" in
    "$repo_root/"*) printf 'output directory must be outside the checkout\n' >&2; exit 2 ;;
esac

# The builder image and the Fedora base it was built from come from the pin;
# no image is ever resolved from a moving tag.
pin_file="$repo_root/packaging/rpm/builder/image.json"
fedora_repository=registry.fedoraproject.org/fedora
builder_repository=ghcr.io/andre-motta/tongs-rpm-builder
builder_workflow=andre-motta/tongs/.github/workflows/rpm-builder-image.yml
digest_pattern='^sha256:[0-9a-f]{64}$'
source_builder=localhost/tongs-desktop-rpm-source:issue-52
dependency_builder=localhost/tongs-python-rpms:issue-85
accepted="$output_dir/accepted-download"
dependency_prepared="$output_dir/dependency-prepared"
dependency_srpms="$output_dir/dependency-srpms"
dependency_rpms="$output_dir/dependency-rpms"
companion_consumer_rpms="$output_dir/companion-consumer-rpms"
prepared="$output_dir/prepared"
srpms="$output_dir/srpms"
rpms="$output_dir/rpms"
install_evidence="$output_dir/install"
test_plugin_rpms="$output_dir/test-plugin-rpms"
previous_repo="$output_dir/install-repo/previous"
final_repo="$output_dir/install-repo/final"
mkdir -p -- "$accepted" "$dependency_prepared" "$dependency_srpms" \
    "$dependency_rpms" "$companion_consumer_rpms" "$prepared" "$srpms" "$rpms" \
    "$install_evidence" "$test_plugin_rpms" "$previous_repo" "$final_repo"
{
    printf 'GITHUB_RUN_ATTEMPT=%s\n' "${GITHUB_RUN_ATTEMPT:-unknown}"
    printf 'GITHUB_RUN_ID=%s\n' "${GITHUB_RUN_ID:-unknown}"
    printf 'GITHUB_SHA=%s\n' "${GITHUB_SHA:-unknown}"
    printf 'RUNNER_ARCH=%s\n' "${RUNNER_ARCH:-unknown}"
    printf 'RUNNER_ENVIRONMENT=%s\n' "${RUNNER_ENVIRONMENT:-unknown}"
    printf 'source_sha=%s\n' "$source_sha"
    printf 'builder_mode=%s\n' "$([[ -n "$local_builder" ]] && printf local || printf pinned)"
} >"$output_dir/runner.env"
podman version --format json >"$output_dir/podman-version.json"
podman info --format json >"$output_dir/podman-info.json"

pin_field() {
    python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
        "$pin_file" "$1"
}
builder_label() {
    podman image inspect --format "{{index .Labels \"$1\"}}" "$builder_image"
}
if [[ -n "$local_builder" ]]; then
    builder_image=$local_builder
    podman image exists "$builder_image" || {
        printf 'local builder image not found: %s\n' "$builder_image" >&2
        exit 2
    }
    base_digest=$(builder_label org.opencontainers.image.base.digest)
    printf 'local builder %s; no pull and no attestation check\n' "$builder_image" \
        >"$output_dir/builder-local.txt"
else
    # Refuse an unpublished or malformed pin before anything is pulled.
    python3 -I "$repo_root/scripts/pin_rpm_builder.py" --pin "$pin_file" \
        --check --require-published
    cp -- "$pin_file" "$output_dir/builder-pin.json"
    [[ "$(pin_field image)" == "$builder_repository" ]] || {
        printf 'the pin names another builder image\n' >&2
        exit 1
    }
    builder_digest=$(pin_field digest)
    pinned_commit=$(pin_field source_commit)
    base_digest=$(pin_field base_digest)
    [[ "$builder_digest" =~ $digest_pattern ]] || { printf 'invalid builder digest\n' >&2; exit 1; }
    builder_image="$builder_repository@$builder_digest"
    # Only an image this repository's builder workflow built and attested on
    # main, from the commit the pin records, may build the RPMs.  The image
    # is pulled by digest alone, after its attestation verifies.
    gh attestation verify "oci://$builder_image" \
        --repo andre-motta/tongs \
        --signer-workflow "$builder_workflow" \
        --source-ref refs/heads/main \
        --source-digest "$pinned_commit" \
        --deny-self-hosted-runners \
        --format json >"$output_dir/builder-attestation.json"
    podman pull --quiet "$builder_image" >"$output_dir/builder-image.pull.txt"
    [[ "$(builder_label org.opencontainers.image.revision)" == "$pinned_commit" ]] || {
        printf 'the builder image revision label differs from the pin\n' >&2
        exit 1
    }
fi
podman image inspect "$builder_image" >"$output_dir/builder-image.inspect.json"
[[ "$base_digest" =~ $digest_pattern ]] || {
    printf 'invalid Fedora base digest: %s\n' "$base_digest" >&2
    exit 1
}
[[ "$(builder_label org.opencontainers.image.base.digest)" == "$base_digest" ]] || {
    printf 'the builder image was not built from the pinned Fedora base\n' >&2
    exit 1
}
# The clean install container stays plain Fedora, pinned to the builder's base.
base_image="$fedora_repository@$base_digest"
podman pull --quiet "$base_image" >"$output_dir/base-image.pull.txt"
podman image inspect "$base_image" >"$output_dir/base-image.inspect.json"
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --entrypoint /bin/bash "$base_image" -euo pipefail -c '
        required=(bash cat cut dnf find grep install mkdir rpm sed sha256sum sort stat tee)
        for command in "${required[@]}"; do
            resolved=$(command -v "$command")
            test -n "$resolved"
            printf "%s|%s\n" "$command" "$resolved"
        done
    ' >"$output_dir/base-prerequisite-probe.txt"

fixture_arguments=()
if [[ -n "$accepted_input" ]]; then
    accepted_archive=$(cd -- "$accepted_input" && pwd)
else
    payload_manifest="$script_dir/manifest.json"
    artifact_name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accepted_desktop"]["artifact_name"])' "$payload_manifest")
    artifact_run=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accepted_desktop"]["run_id"])' "$payload_manifest")
    gh run download "$artifact_run" --repo andre-motta/tongs --name "$artifact_name" --dir "$accepted"
    accepted_archive="$accepted/archive"
    [[ -d "$accepted_archive" ]] || { printf 'downloaded issue 51 artifact lacks archive directory\n' >&2; exit 1; }
    fixture_arguments=(--allow-reviewed-fixture)
fi
cp -- "$payload_manifest" "$output_dir/payload-input-contract.json"

podman build --pull=never --build-arg "BASE_IMAGE=$builder_image" \
    --tag "$source_builder" --file "$script_dir/Containerfile" "$script_dir" \
    2>&1 | tee "$output_dir/source-builder.log"
podman image inspect "$source_builder" >"$output_dir/source-builder.inspect.json"

podman run --rm --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$output_dir:/evidence:rw,Z" \
    "$source_builder" python3 /checkout/packaging/rpm/desktop/audit_providers.py \
        --manifest /checkout/packaging/rpm/desktop/manifest.json \
        --output /evidence/fedora-provider-audit.json
cp -- "$output_dir/fedora-provider-audit.json" \
    "$install_evidence/fedora-provider-audit.json"

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$accepted_archive:/accepted:ro,z" \
    --volume "$prepared:/prepared:rw,Z" \
    --volume "$output_dir/payload-input-contract.json:/payload-contract.json:ro,z" \
    "$source_builder" \
    python3 /checkout/packaging/rpm/desktop/prepare_sources.py \
        --checkout /checkout --accepted-dir /accepted --output-dir /prepared \
        --manifest /payload-contract.json "${fixture_arguments[@]}"
core_version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["pep440_version"])' \
    "$prepared/prepared-inputs.json")
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$prepared:/prepared:ro,z" \
    --volume "$output_dir:/evidence:rw,Z" "$source_builder" \
    /checkout/packaging/rpm/desktop/preflight_core_version.sh \
        --prepared-dir /prepared --expected-version "$core_version" \
        --output /evidence/core-version-preflight.log

podman build --pull=never --build-arg "BASE_IMAGE=$builder_image" \
    --tag "$dependency_builder" --file "$repo_root/packaging/rpm/python-dependencies/Containerfile" \
    "$repo_root/packaging/rpm/python-dependencies" 2>&1 | tee "$output_dir/dependency-builder.log"
podman image inspect "$dependency_builder" >"$output_dir/dependency-builder.inspect.json"

podman run --rm --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$dependency_prepared:/prepared:rw,Z" \
    "$dependency_builder" python3 /checkout/packaging/rpm/python-dependencies/prepare_sources.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --output-dir /prepared
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$dependency_prepared:/prepared:ro,z" \
    --volume "$dependency_srpms:/srpms:rw,Z" "$dependency_builder" \
    /checkout/packaging/rpm/python-dependencies/build_rpms.sh \
        --source-dir /prepared --output-dir /srpms
"$repo_root/packaging/rpm/python-dependencies/rebuild_srpms.sh" \
    --base-image "$builder_image" --checkout "$repo_root" --source-sha "$source_sha" \
    --srpm-dir "$dependency_srpms" --output-dir "$dependency_rpms"
podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$dependency_rpms:/rpms:ro,z" \
    --volume "$companion_consumer_rpms:/selected:rw,Z" \
    --volume "$output_dir:/evidence:rw,Z" "$source_builder" \
    python3 /checkout/packaging/rpm/desktop/select_companion_rpms.py \
        --manifest /checkout/packaging/rpm/python-dependencies/manifest.json \
        --rpm-dir /rpms --output-dir /selected \
        --report /evidence/companion-rpm-selection.json

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$prepared:/prepared:ro,z" \
    --volume "$srpms:/srpms:rw,Z" "$source_builder" \
    /checkout/packaging/rpm/desktop/build_srpms.sh \
        --prepared-dir /prepared --output-dir /srpms
"$script_dir/rebuild_srpms.sh" \
    --base-image "$builder_image" --checkout "$repo_root" --source-sha "$source_sha" \
    --srpm-dir "$srpms" --companion-rpm-dir "$companion_consumer_rpms" \
    --output-dir "$rpms"

podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" --volume "$test_plugin_rpms:/output:rw,Z" \
    "$source_builder" /checkout/packaging/rpm/desktop/build_test_plugin.sh \
        --source-dir /checkout/packaging/rpm/desktop/test-plugin \
        --license /checkout/LICENSE --output-dir /output
find "$companion_consumer_rpms" -maxdepth 1 -type f -name '*.rpm' \
    -exec cp -- {} "$previous_repo/" \; -exec cp -- {} "$final_repo/" \;
find "$rpms/previous" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' \
    ! -name '*-debuginfo-*' -exec cp -- {} "$previous_repo/" \;
find "$rpms/final" -maxdepth 1 -type f -name '*.rpm' ! -name '*.src.rpm' \
    ! -name '*-debuginfo-*' -exec cp -- {} "$final_repo/" \;
find "$test_plugin_rpms" -maxdepth 1 -type f -name '*.rpm' \
    -exec cp -- {} "$previous_repo/" \; -exec cp -- {} "$final_repo/" \;
for repository in "$previous_repo" "$final_repo"; do
    podman run --rm --network=none --cap-drop=all --security-opt=no-new-privileges \
        --volume "$repository:/repo:rw,Z" "$source_builder" createrepo_c /repo
done

podman run --rm --security-opt=no-new-privileges \
    --volume "$repo_root:/checkout:ro,z" \
    --volume "$companion_consumer_rpms:/companions:ro,z" \
    --volume "$rpms/previous:/previous:ro,z" --volume "$rpms/final:/final:ro,z" \
    --volume "$previous_repo:/previous-repo:ro,z" --volume "$final_repo:/final-repo:ro,z" \
    --volume "$prepared:/prepared:ro,z" --volume "$install_evidence:/evidence:rw,Z" \
    "$base_image" /checkout/packaging/rpm/desktop/install_and_verify.sh \
        --companion-dir /companions --previous-dir /previous --final-dir /final \
        --previous-repo /previous-repo --final-repo /final-repo \
        --prepared-dir /prepared --evidence-dir /evidence --checkout /checkout \
        --core-version "$core_version"

find "$output_dir" -type f ! -name ALL-SHA256SUMS -print0 | sort -z | xargs -0 sha256sum \
    | sed "s#${output_dir}/##" >"$output_dir/ALL-SHA256SUMS"
