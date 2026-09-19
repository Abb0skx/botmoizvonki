<?php
// Run explicitly through the server administrator's SSH/container access.
// Uses Coolify's own models/deployment queue; touches one fixed application.
require '/var/www/html/vendor/autoload.php';
$laravel = require '/var/www/html/bootstrap/app.php';
$laravel->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$resource = App\Models\Application::where('uuid', 'nylgfmvjodgie9dga7ngprgl')->firstOrFail();
$mode = getenv('ASR_RELEASE_ACTION') ?: 'inspect';
$backup = '/tmp/texnikach-transcription-coolify-before-large-v3-20260919.json';
$settings = [
    'TRANSCRIPTION_ENABLED' => 'true',
    'TRANSCRIPTION_WORKER_MODE' => 'external',
    'LOCAL_WHISPER_MODEL' => 'large-v3',
];

if ($mode === 'status') {
    $latest = App\Models\ApplicationDeploymentQueue::where('application_id', $resource->id)
        ->orderByDesc('id')->first();
    echo json_encode($latest ? $latest->only(['deployment_uuid', 'status', 'commit', 'created_at', 'updated_at']) : []) . "\n";
    exit;
}

if ($mode === 'inspect' || $mode === 'backup') {
    $values = [];
    foreach ($settings as $key => $_) {
        $env = $resource->environment_variables->where('key', $key)->first();
        $values[$key] = $env ? ['id' => $env->id, 'value' => $env->value,
            'is_runtime' => $env->is_runtime, 'is_buildtime' => $env->is_buildtime] : null;
    }
    $data = ['uuid' => $resource->uuid, 'git_branch' => $resource->git_branch,
             'git_commit_sha' => $resource->git_commit_sha, 'environment' => $values];
    if ($mode === 'backup') {
        if (file_exists($backup)) { throw new RuntimeException('Backup already exists; do not overwrite'); }
        file_put_contents($backup, json_encode($data, JSON_PRETTY_PRINT), LOCK_EX);
        chmod($backup, 0600);
        echo "Backup saved\n";
    } else {
        echo json_encode($data) . "\n";
    }
    exit;
}

if ($mode === 'restore-config') {
    $data = json_decode(file_get_contents($backup), true, flags: JSON_THROW_ON_ERROR);
    if ($data['uuid'] !== $resource->uuid) { throw new RuntimeException('Backup target mismatch'); }
    Illuminate\Support\Facades\DB::transaction(function () use ($resource, $data) {
        $resource->git_branch = $data['git_branch'];
        $resource->git_commit_sha = $data['git_commit_sha'];
        $resource->save();
        foreach ($data['environment'] as $key => $old) {
            $env = $resource->environment_variables()->where('key', $key)->where('is_preview', false)->first();
            if (!$old) { if ($env) { $env->delete(); } continue; }
            if (!$env) { throw new RuntimeException('Existing setting missing; inspect manually'); }
            foreach (['value', 'is_runtime', 'is_buildtime'] as $field) { $env->$field = $old[$field]; }
            $env->save();
        }
    });
    echo "Previous config restored; runtime rollback is a separate explicit action\n";
    exit;
}
if ($mode !== 'deploy') { throw new RuntimeException('Unknown release action'); }
$commit = getenv('ASR_RELEASE_COMMIT');
if (!preg_match('/^[a-f0-9]{40}$/', $commit ?: '')) { throw new RuntimeException('Full commit required'); }
if (!file_exists($backup)) { throw new RuntimeException('Create and export backup before deployment'); }
if (!in_array($resource->git_branch, ['main', 'codex/local-call-transcription-20260918'], true)) {
    throw new RuntimeException('Application branch changed; inspect before deploying');
}
Illuminate\Support\Facades\DB::transaction(function () use ($resource, $settings) {
    $resource->git_branch = 'codex/local-call-transcription-20260918';
    $resource->save();
    foreach ($settings as $key => $value) {
        $env = $resource->environment_variables()->where('key', $key)->where('is_preview', false)->first();
        if ($env) {
            $env->value = $value;
            $env->is_runtime = true;
            $env->is_buildtime = false;
            $env->save();
        } else {
            $resource->environment_variables()->create([
                'key' => $key, 'value' => $value, 'is_preview' => false,
                'is_literal' => true, 'is_multiline' => false,
                'is_runtime' => true, 'is_buildtime' => false,
                'resourceable_type' => get_class($resource), 'resourceable_id' => $resource->id,
            ]);
        }
    }
});
$result = queue_application_deployment(application: $resource->fresh(),
    deployment_uuid: new_public_id(), commit: $commit, force_rebuild: false, is_api: false);
echo json_encode(['status' => $result['status'] ?? null,
                 'deployment_uuid' => $result['deployment_uuid'] ?? null]) . "\n";
