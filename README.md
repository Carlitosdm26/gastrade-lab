# GastradeLab

GastradeLab es un simulador de trading automático de acciones. Su interfaz web controla y observa un servicio de fondo; las operaciones se procesan en el servidor, no en la pestaña del navegador. Cerrar la página no detiene un experimento.

> **Solo paper trading.** La aplicación no conecta con brokers ni envía órdenes reales. Los resultados simulados no predicen ganancias futuras.

## Funcionamiento

1. Configura el saldo virtual, entre una y tres acciones, comisión, porcentaje de capital, intervalo de velas y duración.
2. Pulsa **Iniciar experimento**. La duración predeterminada es 30 días; se puede elegir entre 7, 14, 30 y 45 días.
3. Un worker consulta las cotizaciones en segundo plano, guarda las velas completas y ejecuta la estrategia únicamente cuando aparece una vela nueva y cerrada.
	El dashboard conserva hasta 120 consultas recientes por acción para comparar cómo cambia el precio.
4. Por defecto utiliza un cruce de medias móviles (7 y 21 periodos), saldo inicial de 500 EUR, acciones AAPL/MSFT/NVDA, velas de 5 minutos, comisión del 0,1 % y un máximo de capital asignado del 80 %. Todos esos valores se configuran antes de iniciar.
5. Desde el panel puedes pausar, reanudar o finalizar. Al alcanzar la duración se cierra la sesión automáticamente y su resultado permanece en el historial.

Una simulación pausada no acumula tiempo activo. Si el contenedor se reinicia, el estado, las posiciones, las operaciones y las velas se recuperan de la base de datos. El plazo se mide en tiempo natural; si el host estuvo apagado, el worker no puede reconstruir operaciones que no ejecutó mientras estaba fuera.

## Datos de mercado

La versión inicial consulta cotizaciones y velas públicas de Yahoo Finance para AAPL, MSFT, NVDA, AMZN y TSLA. Los precios se reciben en USD y se convierten a EUR con el par EUR/USD. No se requiere una clave API.

El endpoint público de Yahoo Finance no ofrece un SLA y puede retrasar, limitar o cambiar sus respuestas. El estado de la bolsa, la disponibilidad de datos y la frecuencia efectiva dependen de ese proveedor. Sin datos válidos no se generan trades. Fuera del horario bursátil, fines de semana y festivos no se esperan velas nuevas ni operaciones.

## Despliegue en Linux

El contenedor usa Python 3.12 y está preparado para Linux `amd64` y `arm64` de 64 bits. Puede desplegarse en ZimaOS con Docker Compose o en Raspberry Pi OS de 64 bits con Docker Engine y el complemento Compose instalados.

1. Copia el proyecto al host Linux.
2. Copia `.env.example` como `.env` y ajusta el puerto o `DATABASE_URL` si hace falta.
3. Inicia el servicio:

```sh
docker compose up -d --build
```

4. Abre `http://IP_DEL_HOST:8080`.
5. Comprueba el estado con `docker compose ps` y consulta los registros con `docker compose logs -f gastradelab`.

Compose aplica `restart: unless-stopped`; el daemon de Docker del host debe habilitarse al arranque y el dispositivo debe permanecer encendido y conectado a Internet. El volumen persistente conserva la base aunque se actualice o recree el contenedor.

### Inicio local sin Docker

Requiere Python 3.12 o posterior:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL=sqlite:///./data/papertrade.db
uvicorn app:app --host 0.0.0.0 --port 8080
```

En Windows PowerShell, activa el entorno con `.venv\Scripts\Activate.ps1` y configura la variable con `$env:DATABASE_URL = 'sqlite:///./data/papertrade.db'`.

## Base de datos

Por defecto, SQLAlchemy usa SQLite en `/data/papertrade.db` dentro del contenedor. Al iniciar, `Base.metadata.create_all()` crea las tablas que falten: experimentos, cotizaciones, velas, posiciones, operaciones, cursores de estrategia y valoraciones.

Para usar PostgreSQL, configura `DATABASE_URL` en `.env`, por ejemplo:

```env
DATABASE_URL=postgresql+psycopg://USUARIO:CONTRASENA@SERVIDOR:5432/gastradelab
```

La aplicación crea las tablas en la base indicada. Configurar otra base **no copia** los registros existentes de SQLite; la migración de datos se debe realizar por separado. Mantén `.env` privado y no añadas credenciales al repositorio.

## Pruebas

```sh
pip install -r requirements-dev.txt
pytest -q
```

La suite verifica señales y comisiones, el control de experimentos, la creación del esquema, el historial de cotizaciones y que el worker solo opere después de una vela nueva cerrada.

## Estructura

- `app.py`: API web, control de sesiones y resúmenes.
- `worker.py`: consulta de mercado, persistencia de velas y ejecución simulada.
- `database.py`: modelos SQLAlchemy y creación automática del esquema.
- `market_data.py`: cliente HTTP del proveedor de mercado.
- `strategy.py`: lógica de medias móviles y cartera.
- `index.html`, `styles.css`, `appClient.js`: panel web.
- `tests/`: pruebas automatizadas.
- `Dockerfile`, `docker-compose.yml`: despliegue Linux.
