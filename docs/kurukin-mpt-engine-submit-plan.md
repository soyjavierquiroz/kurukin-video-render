# Kurukin MPT engine submit plan

Este checkpoint no ejecuta MoneyPrinterTurbo.

`app/custom/mpt_engine_submitter.py` solo prepara un plan de submit validado a
partir de specs Kurukin que ya pasan por
`app.custom.mpt_engine_bridge.build_validated_mpt_video_task_from_kurukin_job()`.

El target futuro del plan es el camino nativo MPT:

- API: `/api/v1/videos`
- service: `app.services.task.start`

En esta fase esos paths son metadata de planificacion. No se llaman.

## Guardrails

- El modo por defecto es `dry_run`.
- `dry_run=True` no usa executor.
- Submit real requiere autorizacion explicita.
- Submit real requiere `KURUKIN_ENABLE_MPT_ENGINE_SUBMIT=1`.
- Submit real requiere un executor inyectado.
- Esta rama no implementa un submit real a `task.start`.

## No side effects

El submit plan:

- no llama proveedores;
- no llama Pexels/Pixabay/Coverr;
- no descarga assets;
- no renderiza;
- no crea task;
- no crea pending;
- no llama `/api/v1/videos`;
- no llama `app.services.task.start`;
- no ejecuta runner;
- no ejecuta ffmpeg ni ffprobe.

## Proximo paso

El proximo paso es un smoke submit real controlado al motor MPT, solo con
autorizacion explicita y manteniendo el executor real fuera de esta rama.
