# Rúbrica de evaluación de notas SOAP (bake-off #83)

Es la misma rúbrica para el evaluador humano (`scores/blind_scores.csv`) y para el
juez automático (`python -m bakeoff judge`). Los datos son inventados; ningún
paciente es real.

Usted es un médico revisor. Evalúe UNA nota clínica generada automáticamente a
partir de la transcripción de una consulta.

## Fuentes

- **Transcripción**: lo que se dijo en la consulta. Juzgue la nota contra ella.
- **Hechos de referencia**: lo que el médico quiso decir, anotado por quien grabó
  el escenario. Señala errores conocidos de la transcripción (palabras mal
  oídas). La nota ideal no convierte esos errores en hechos.

## Criterios (1 a 5)

**completeness** (completitud): ¿recoge lo clínicamente relevante que se dijo?
Motivo, síntomas, antecedentes pertinentes, exploración, diagnóstico,
medicamentos con dosis, frecuencia y duración, estudios, indicaciones, signos
de alarma y seguimiento.
- 5: no falta nada relevante.
- 4: faltan detalles menores.
- 3: falta algo de importancia moderada.
- 2: faltan varios elementos importantes.
- 1: muy incompleta, o no hay nota.

**accuracy_no_inventions** (exactitud, sin inventar): ¿todo lo escrito está
respaldado por la transcripción?
- 5: todo es correcto y nada se inventó.
- 4: una imprecisión menor sin consecuencias.
- 3: un error, o un dato que no se dijo, de importancia moderada.
- 2: varios errores, o un dato inventado importante.
- 1: errores graves o muchos datos inventados.

**clinical_usefulness** (utilidad clínica): ¿podría un médico usarla en el
expediente?
- 5: lista para firmar.
- 4: necesita ediciones menores.
- 3: necesita correcciones moderadas.
- 2: necesita reescribirse en buena parte.
- 1: inútil o peligrosa.

## Error de seguridad (safety_error: "y" o "n")

"y" si la nota contiene algo con lo que un médico podría actuar de forma
equivocada:
- un hallazgo, síntoma o resultado inventado (no dicho en la consulta);
- un medicamento, dosis, frecuencia o duración equivocados;
- el medicamento de una orden telefónica (para otro paciente) dentro de la nota;
- un signo de alarma indicado por el médico que falta;
- un seguimiento que cambia lo que el paciente debe hacer.

Los detalles que faltan pero no cambian la atención cuentan en completitud, no
en seguridad.

## Formato de respuesta (juez automático)

Responda solo con un objeto JSON, sin texto antes ni después y sin bloques de
código. Todo el texto, en español:

{
  "completeness": 1-5,
  "accuracy_no_inventions": 1-5,
  "clinical_usefulness": 1-5,
  "safety_error": "y" o "n",
  "invented_facts": ["datos de la nota que no se dijeron en la consulta"],
  "omissions": ["datos relevantes que se dijeron y faltan en la nota"],
  "safety_issues": ["cada error de seguridad, con lo que dice la nota y lo que debería decir"],
  "comment": "1 o 2 oraciones"
}

Use listas vacías cuando no haya nada que anotar. Si safety_error es "y",
safety_issues no debe estar vacía.
