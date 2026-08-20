# Deploy gratuito

A aplicacao principal e `app.py`. Os scripts `app_excel.py`, `auditoria.py` e `empresas.py` ficam apenas para consulta; as funcoes de auditoria e empresas ja estao integradas no app web.

## Banco

Use um PostgreSQL gratuito em Neon ou Supabase. Copie a URL de conexao com `sslmode=require` para `DATABASE_URL`. Nao use o SQLite local em producao, pois o armazenamento do servidor pode ser perdido.

## Render

1. Publique esta pasta em um repositorio privado no GitHub.
2. No Render, crie um Web Service apontando para o repositorio.
3. O arquivo `render.yaml` define o build e o comando de inicializacao.
4. Cadastre os valores secretos marcados como `sync: false`.
5. Informe a URL PostgreSQL em `DATABASE_URL`.
6. Mantenha `FLASK_DEBUG=0`.

O frontend ja e servido pelo Flask. Nao e necessario um segundo servico frontend.

## Desenvolvimento local

O arquivo `.env` local e carregado automaticamente. Para iniciar:

```powershell
python app.py
```

Em producao, o Render usa o `Procfile`/`render.yaml` com Gunicorn.
