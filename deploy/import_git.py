"""Import real cloud history without touching the existing dirty working tree."""
import argparse
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SHA='afcfffff56c8a69dc9b4a3bef40250eab2d026be'
BASE='97ec6feea37b853e0259ece1b1e67e80b3d4b992'
parser=argparse.ArgumentParser()
parser.add_argument('--repo',required=True)
parser.add_argument('--destination',required=True)
args=parser.parse_args()
repo=Path(args.repo).expanduser().resolve();worktree=Path(args.destination).expanduser().resolve()
def git(*arguments,where=repo):
    return subprocess.check_output(['git','-C',str(where),*arguments])
if worktree.exists():raise SystemExit('Refusing to reuse existing deployment directory')
if git('rev-parse','--show-toplevel').decode().strip()!=str(repo):raise SystemExit('--repo must be the repository root')
if worktree.is_relative_to(repo):
    relative=str(worktree.relative_to(repo))+'/'
    ignored=subprocess.run(['git','-C',str(repo),'check-ignore','--quiet',relative])
    if ignored.returncode!=0:
        raise SystemExit('Destination must already be excluded by the parent repository (local .git/info/exclude); tracked files will not be changed')
head_before=git('rev-parse','HEAD');status_before=git('status','--porcelain=v1','-z')
bundle=ROOT/'handoff/cloud-rag-integration.bundle'
patch=ROOT/'handoff/mac-local.patch'
heads=git('bundle','list-heads',str(bundle)).decode()
if f'{SHA} refs/heads/cloud/rag-integration' not in heads:raise SystemExit('Unexpected bundle head')
git('bundle','verify',str(bundle))
# Clone into an independent nested repository; import nothing into the dirty parent.
subprocess.run(['git','clone','--branch','cloud/rag-integration',str(bundle),str(worktree)],check=True)
assert git('rev-parse','HEAD',where=worktree).decode().strip()==SHA
git('cat-file','-e',BASE+'^{commit}',where=worktree)
git('merge-base','--is-ancestor',BASE,SHA,where=worktree)
local_branch='local/mac-deepseek-qwen'
git('switch','-c',local_branch,where=worktree)
git('apply','--check',str(patch),where=worktree)
git('apply','--index',str(patch),where=worktree)
assert git('rev-parse','HEAD')==head_before,'Existing HEAD changed unexpectedly'
assert git('status','--porcelain=v1','-z')==status_before,'Existing dirty worktree changed unexpectedly'
print('Imported authentic cloud history; original HEAD and dirty working tree unchanged.')
print('Independent deployment clone:',worktree)
print('Local adaptations are staged on',local_branch,'; commit them locally after review.')
