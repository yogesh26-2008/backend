git checkout main
git pull origin main --rebase
git merge backend -m "merge backend into main"
git push origin main
git checkout backend
