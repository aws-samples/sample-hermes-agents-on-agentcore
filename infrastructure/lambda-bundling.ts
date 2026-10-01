import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import * as path from 'node:path';
import { AssetHashType, IgnoreMode, aws_lambda as lambda } from 'aws-cdk-lib';
import { Construct } from 'constructs';

const ROOT = path.join(__dirname, '..');

export const PORTAL_LAMBDA_RUNTIME = lambda.Runtime.PYTHON_3_12;
export const PORTAL_LAMBDA_ARCHITECTURE = lambda.Architecture.ARM_64;
// Wheels are selected for the Lambda target, not the build host, so a macOS or x86 machine
// produces the same arm64 layer. Source distributions are refused rather than built for the host.
const PYTHON_PLATFORM = 'aarch64-manylinux2014';
const PYTHON_VERSION = '3.12';
// Used only by the Docker fallback when uv is not installed locally.
const DOCKER_UV_VERSION = '0.8.6';

/**
 * Commands that install the `lambda` dependency group exactly as pinned (with hashes) in uv.lock.
 * The project itself is not installed; handler code ships in a separate asset.
 */
export function lambdaInstallCommands(source: string, output: string): string[][] {
  const requirements = path.posix.join(output, 'requirements.txt');
  return [
    ['uv', 'export', '--project', source, '--frozen', '--only-group', 'lambda',
      '--no-emit-project', '--no-header', '--quiet', '--output-file', requirements],
    ['uv', 'pip', 'install', '--quiet', '--require-hashes', '--no-deps', '--only-binary', ':all:',
      '--python-platform', PYTHON_PLATFORM, '--python-version', PYTHON_VERSION,
      '--target', path.posix.join(output, 'python'), '-r', requirements],
  ];
}

function dependencyHash(): string {
  const hash = createHash('sha256');
  for (const file of ['pyproject.toml', 'uv.lock']) hash.update(readFileSync(path.join(ROOT, file)));
  hash.update(JSON.stringify([PORTAL_LAMBDA_RUNTIME.name, PYTHON_PLATFORM, lambdaInstallCommands('', '')]));
  return hash.digest('hex');
}

function quote(argument: string): string {
  return `'${argument.replace(/'/g, `'\\''`)}'`;
}

/** Third-party packages shared by every portal handler, built from uv.lock. */
export function portalDependencyLayer(scope: Construct, id: string): lambda.LayerVersion {
  return new lambda.LayerVersion(scope, id, {
    description: 'Portal Python dependencies pinned by uv.lock (lambda dependency group)',
    compatibleRuntimes: [PORTAL_LAMBDA_RUNTIME],
    compatibleArchitectures: [PORTAL_LAMBDA_ARCHITECTURE],
    code: lambda.Code.fromAsset(ROOT, {
      // Only the lock inputs determine the layer; code edits must not rebuild it.
      assetHashType: AssetHashType.CUSTOM, assetHash: dependencyHash(),
      bundling: {
        local: {
          tryBundle(outputDir: string) {
            if (spawnSync('uv', ['--version'], { stdio: 'ignore' }).status !== 0) return false;
            for (const [command, ...args] of lambdaInstallCommands(ROOT, outputDir)) {
              const result = spawnSync(command, args, { stdio: 'inherit' });
              if (result.status !== 0) throw new Error(`Lambda layer build failed: ${command} ${args.join(' ')}`);
            }
            return true;
          },
        },
        image: PORTAL_LAMBDA_RUNTIME.bundlingImage,
        platform: 'linux/arm64',
        // CDK runs the container as the host user, which has no writable home directory.
        environment: { HOME: '/tmp', UV_CACHE_DIR: '/tmp/uv-cache' },
        command: ['bash', '-euo', 'pipefail', '-c', [
          `pip install --quiet --target /tmp/uv uv==${DOCKER_UV_VERSION}`,
          ...lambdaInstallCommands('/asset-input', '/asset-output')
            .map(([command, ...args]) => [`/tmp/uv/bin/${command}`, ...args].map(quote).join(' ')),
        ].join(' && ')],
      },
    }),
  });
}

/** First-party handler code only: `backend/` and `common/`. */
export function portalHandlerCode(): lambda.Code {
  return lambda.Code.fromAsset(ROOT, {
    ignoreMode: IgnoreMode.DOCKER,
    exclude: ['*', '!backend', '!common', '**/__pycache__', '**/*.pyc', 'backend/Dockerfile*'],
  });
}
