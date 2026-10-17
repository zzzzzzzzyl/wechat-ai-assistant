# Git 与命令行速查

## merge 和 rebase 的区别

merge 会生成一个合并提交，历史里能看到分叉；rebase 把提交"搬"到目标分支上，
历史变成一条直线但没有合并记录。
原则：**公共分支上用 merge，自己的特性分支在推送前用 rebase 整理历史**。
已经推送并被别人拉走的分支，不要 rebase。
常见问法：rebase 和 merge 选哪个、rebase 会把别人的提交弄丢吗、什么时候不能 rebase

## reset 的三种模式

`git reset --soft` 只回退提交，改动留在暂存区；
`--mixed`（默认）回退提交和暂存区，改动留在工作区；
`--hard` 三者全回退，**工作区的改动会丢**，用之前先 `git stash` 或确认没有未提交内容。
常见问法：reset soft mixed hard 区别、怎么撤销 commit 但保留代码、reset hard 能恢复吗

## 撤销已经推送的提交

用 `git revert <commit>`：它**新建一个反向提交**，历史不被改写，别人拉代码不会冲突。
`reset` 改写历史，只适合还没推送的提交。
常见问法：已经 push 了怎么撤销、revert 和 reset 区别、git 回滚远程分支

## 救回误删的分支或提交

`git reflog` 会列出 HEAD 的移动记录，找到那次的 commit hash 之后
`git branch <名字> <hash>` 就能救回来。reflog 默认保留 90 天。
常见问法：分支删了怎么恢复、commit 丢了怎么办、reflog 是什么

## stash 怎么用

`git stash` 把当前未提交的改动收起来，`git stash pop` 取回并删除那条记录，
`git stash apply` 取回但保留记录。多个 stash 用 `git stash list` 看、`git stash pop stash@{2}` 指定。
常见问法：stash 怎么用、stash pop 和 apply 区别、切分支前代码怎么办

## 看某一行是谁改的

`git blame <文件>` 每行标出 commit、作者和时间；`git log -p -- <文件>` 看这个文件的完整修改历史；
`git log -S"关键词"` 按"哪次提交增删了这个字符串"来搜。
常见问法：怎么查某行谁改的、blame 怎么用、代码里某个函数是谁加的

## 国内装包慢怎么办

pip 换镜像源：`pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple`。
conda 换源同理。Python 环境建议一个项目一个 venv，导出依赖用 `pip freeze > requirements.txt`，
别人装用 `pip install -r requirements.txt`。
常见问法：pip 太慢怎么办、怎么换源、requirements.txt 怎么生成
