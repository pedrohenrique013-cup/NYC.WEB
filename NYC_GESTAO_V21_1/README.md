# NYC Gestão V21.1

Sistema local de estoque e vendas. Para iniciar, extraia o ZIP em uma pasta normal e execute `INICIAR_SISTEMA\INICIAR.bat`.

## Primeiro uso

1. Confira no navegador se a Central mostra o banco e a pasta corretos.
2. Em instalação realmente nova, prepare a base vazia e crie o primeiro Proprietário.
3. Se já usa outra pasta, faça backup e siga `documentacao/ATUALIZAR_PARA_V21_1.txt` antes de migrar os dados.
4. Inicie o sistema pela Central. Ela só aceita a API V21.1. Se detectar uma versão antiga na porta 8000, use “Parar sistema” na Central antiga ou feche o terminal que iniciou o servidor antigo; fechar apenas a aba do navegador não o encerra.

## Pagamentos

Em Configurações, cada empresa pode cadastrar sua credencial de pagamento separadamente. A cobrança e a confirmação automáticas estão implementadas para Mercado Pago; os demais bancos ficam em conferência manual. Leia `documentacao/CONFIGURAR_API_POR_LOJA_V21_1.txt` e `documentacao/PIX_MERCADO_PAGO_V21_1.txt`.

## Pastas desta versão

- `INICIAR_SISTEMA`: preparação, verificação da base e início/parada do servidor.
- `backend`: API, contas, autenticação e configurações locais.
- `web`: interface do sistema e do painel de controle.
- `documentacao`: guias da versão atual.
- `dados/backups` e `logs`: criadas pela Central quando necessário.

O ZIP não contém banco, `.env`, tokens, chave de sessão, ambiente virtual nem dados das lojas. Preserve esses arquivos da instalação atual até confirmar a atualização.
