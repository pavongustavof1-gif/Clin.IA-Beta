# Aviso de Privacidad para Pacientes (Beta)
La aplicación denominada Clin.IA™ se encuentra en fase de pruebas (Beta).

### 1. Identidad y domicilio del Responsable
El presente Aviso de Privacidad es emitido por el médico tratante que utiliza la plataforma Clin.IA™ para la generación de notas clínicas ("el Responsable"). El Responsable es el médico que contrata el servicio de Clin.IA™ y que lleva a cabo la consulta en su consultorio o en la clínica en la que ejerce.

Responsable: **[Nombre completo del médico]**
Cédula profesional: **[número]**
Domicilio: **[domicilio del consultorio o clínica]**
Correo electrónico de contacto: **[correo]**

Estos datos se imprimen en la Carta de Consentimiento que el paciente firma al ser registrado.

### 2. Datos personales que se recaban
Datos de identificación: nombre completo, CURP, fecha de nacimiento, edad, sexo, número de expediente clínico.
Datos de salud (datos personales sensibles): motivo de consulta, síntomas, signos vitales, hallazgos de exploración física, diagnóstico con código CIE-11, pronóstico, tratamiento, medicamentos prescritos, estudios solicitados, antecedentes heredofamiliares y personales patológicos, y cualquier otra información clínica mencionada durante la consulta.
Datos de la consulta: grabación de audio (eliminada automáticamente tras la transcripción), transcripción de texto (eliminada automáticamente tras la confirmación de la nota clínica), fecha y hora exacta de la consulta con zona horaria del dispositivo.
Datos de consentimiento: registro del consentimiento otorgado por el paciente, incluyendo fecha y hora exacta.

### 3. Finalidades del tratamiento de datos personales
Finalidades primarias (necesarias para la prestación del servicio): (1) Transcribir el audio de la consulta médica. (2) Extraer y estructurar la información clínica en formato SOAP conforme a NOM-004-SSA3-2012. (3) Generar la nota clínica de evolución en formato PDF y almacenarla de forma segura. (4) Enviar la nota clínica generada al correo electrónico del médico Responsable. (5) Exportar los datos estructurados en formato JSON para integración con otros sistemas del Responsable.
Los datos personales del paciente no se tratan para finalidades secundarias.

### 4. Encargados del tratamiento de datos personales
Para la prestación del servicio, el Responsable se apoya en Clin.IA™ como encargado del tratamiento. Clin.IA™, a su vez, se apoya en los siguientes subencargados, quienes procesan los datos personales por cuenta del Responsable y conforme a sus instrucciones:

**AssemblyAI Inc. (Estados Unidos)**: transcripción de audio a texto. El audio es eliminado de sus servidores inmediatamente tras el procesamiento exitoso. La transcripción de texto es eliminada de los servidores de Clin.IA™ tras la confirmación de la nota clínica. AssemblyAI no utiliza los datos para entrenamiento de modelos (opt-out activo).

**Google LLC (Estados Unidos):** procesamiento de lenguaje natural mediante Gemini API para la extracción y estructuración de la información clínica a partir de la transcripción.

**Resend (Estados Unidos):** servicio de envío de correo electrónico para la entrega de la nota clínica en PDF al médico Responsable.

**Supabase Pte. Ltd. (Singapur):** infraestructura de base de datos, autenticación y almacenamiento de archivos. Los datos se alojan en **Virginia, Estados Unidos**.

**Render Services, Inc. (Estados Unidos):** alojamiento de la aplicación y procesamiento de las solicitudes.

La comunicación de datos a Clin.IA™ y a sus subencargados constituye una remisión entre responsable y encargado, que no requiere el consentimiento del titular, conforme al artículo 53 del Reglamento de la LFPDPPP. Dichos subencargados están obligados contractualmente a tratar los datos únicamente conforme a las instrucciones recibidas y a mantener medidas de seguridad y confidencialidad, conforme a los artículos 50 a 55 del Reglamento.

Además, la plataforma realiza la siguiente consulta, que no implica la comunicación de datos personales:
**Organización Mundial de la Salud (OMS):** para asignar el código CIE-11 que exige la NOM-004-SSA3-2012, la plataforma consulta la API para CIE-11 de la OMS enviando únicamente el texto del diagnóstico, sin nombre ni ningún otro dato que permita identificar al paciente. Al no poder asociarse a una persona identificada o identificable, esta consulta no constituye una transferencia de datos personales.

### 5. Datos personales sensibles
Los datos de salud recabados constituyen datos personales sensibles en términos del artículo 2, fracción VI de la LFPDPPP vigente. Su tratamiento se realiza con base en el consentimiento expreso del titular y es estrictamente necesario para la prestación del servicio médico.

### 6. Consentimiento
El tratamiento de los datos personales sensibles del paciente requiere su consentimiento expreso y por escrito, otorgado mediante firma autógrafa, firma electrónica o cualquier mecanismo de autenticación, conforme al artículo 8 de la LFPDPPP vigente. El médico Responsable recaba este consentimiento de la siguiente manera:

1. **Al registrar al paciente:** Antes de la primera consulta en la que se utilice Clin.IA™, el personal del Responsable imprime desde la plataforma la Carta de Consentimiento correspondiente a la especialidad y la entrega al paciente. El paciente, o su representante legal, la firma de forma autógrafa, autorizando la grabación de voz de sus consultas y el tratamiento automatizado de sus datos para las finalidades descritas en el presente Aviso. La Carta firmada se integra al expediente clínico.

2. **Antes de cada grabación:** El médico confirma en la plataforma que el consentimiento firmado obra en el expediente y que no ha sido revocado. Esta confirmación queda registrada con fecha y hora exacta para fines de auditoría. El paciente puede negarse a la grabación de cualquier consulta sin que ello afecte la prestación del servicio médico.

3. **Consentimiento para actos médicos:** Cuando el acto médico lo requiera conforme al numeral 10.1.2 de la NOM-004-SSA3-2012, la Carta incluye además el consentimiento informado para dicho acto, con la firma del médico que proporciona la información y de dos testigos, conforme al numeral 10.1.1 de dicha Norma. Este consentimiento para actos médicos es distinto del consentimiento para el tratamiento de datos personales al que se refiere este Aviso.

### 7. Derechos ARCO y Revocación del Consentimiento
El titular o su representante legal tienen derecho a Acceder, Rectificar, Cancelar u Oponerse al tratamiento de sus datos personales, así como a Revocar el consentimiento otorgado, conforme a los artículos 21–34 de la LFPDPPP vigente.

Las solicitudes pueden presentarse directamente ante el Responsable o por escrito al correo admin@clinianotes.com. Clin.IA™, como encargado, turnará al Responsable las solicitudes que reciba por este medio dentro de los 3 días hábiles siguientes a su recepción. La solicitud deberá contener, conforme al artículo 28 de la LFPDPPP vigente:
- Nombre completo del titular y el domicilio o cualquier otro medio para recibir notificaciones.
- Los documentos que acrediten la identidad del titular o, en su caso, la identidad y personalidad de su representante legal.
- La descripción clara y precisa de los datos personales respecto de los que se busca ejercer algún derecho, salvo que se trate del derecho de acceso.
- La descripción del derecho que se desea ejercer.
- Cualquier otro elemento que facilite la localización de los datos, como la fecha de la consulta y el ID de nota (formato SESSION-AAAAMMDD-HHMMSS, visible en el pie de página del PDF de la nota clínica y disponible a través del médico tratante).

**Mecanismos de atención por cada derecho:**
- **Acceso:** El paciente puede solicitar el acceso o copia de cualquier nota médica generada mediante Clin.IA™.
- **Rectificación:** Las correcciones solicitadas a la información clínica se registrarán exclusivamente mediante adéndum oficial adjunto a la nota médica original, la cual no puede ser modificada en su contenido primario conforme a la NOM-004-SSA3-2012.
- **Cancelación y Revocación del Consentimiento:** El titular podrá solicitar la cancelación de sus datos o revocar su consentimiento en cualquier momento. Al proceder la solicitud, los datos clínicos serán bloqueados de forma inmediata. No obstante, no es posible su eliminación definitiva durante el período en que deban conservarse por disposición legal y para la atención médica del titular, conforme al artículo 25, fracciones II y VII de la LFPDPPP vigente y a la NOM-004-SSA3-2012. Transcurrido dicho período de conservación legal, los datos serán eliminados definitivamente.
- **Oposición**: El titular podrá oponerse en cualquier momento al tratamiento de sus datos personales cuando exista una causa legítima derivada de su situación personal, conforme al artículo 26 de la LFPDPPP vigente.

Conforme al artículo 31 de la LFPDPPP vigente, el Responsable comunicará su determinación en un plazo máximo de 20 días hábiles contados desde la recepción de la solicitud y, si resulta procedente, la hará efectiva dentro de los 15 días hábiles siguientes. Ambos plazos podrán ampliarse una sola vez por un periodo igual cuando así lo justifiquen las circunstancias. El ejercicio de estos derechos es gratuito, conforme al artículo 34 de la LFPDPPP vigente.

### 8. Medidas de seguridad
Clin.IA™ implementa las siguientes medidas técnicas y organizativas para proteger sus datos personales:
* Cifrado en tránsito: todas las comunicaciones entre el usuario y la plataforma se realizan mediante protocolo HTTPS con cifrado TLS.
* Minimización de datos: el audio de la consulta es eliminado automáticamente de AssemblyAI tras la transcripción. La transcripción de texto es eliminada de los servidores de Clin.IA™ tras la confirmación de la nota clínica.
* Inmutabilidad de notas clínicas: las notas confirmadas no pueden ser modificadas. Las correcciones se realizan exclusivamente mediante adéndum conforme a NOM-004-SSA3-2012.
* Control de acceso: el acceso a los documentos clínicos está restringido a los usuarios autorizados (médicos y personal administrativo) de la clínica o consultorio del Responsable, con autenticación obligatoria en cada solicitud. Los médicos de una misma clínica pueden consultar las notas de sus colegas para garantizar la continuidad de la atención. La información de cada clínica está aislada de la de las demás.
* Registro de consentimiento: marca de tiempo del consentimiento del paciente registrada en cada consulta para fines de auditoría.
* Cifrado en reposo: la información almacenada se cifra en reposo a nivel de la infraestructura de datos (AES-256).

### 9. Cambios al Aviso de Privacidad
El presente Aviso puede ser modificado en cualquier momento. Cada versión se publica, con su número y fecha, en clinianotes.com/legal, donde permanecen disponibles las versiones anteriores. Cualquier modificación de las finalidades declaradas en el presente Aviso requerirá recabar nuevamente el consentimiento del titular, conforme a la ley vigente. Versión 1.2 (Beta), Octubre 2026.

### 10. Contacto
Para ejercer derechos ARCO o dudas sobre el tratamiento de datos personales: [admin@clinianotes.com](mailto:admin@clinianotes.com)
Para información general sobre la plataforma Clin.IA™: [info@clinianotes.com](mailto:info@clinianotes.com)
Este Aviso de Privacidad ha sido elaborado conforme a la Ley Federal de Protección de Datos Personales en Posesión de los Particulares vigente (publicada en el DOF el 20 de marzo de 2025) y su normatividad aplicable.

---

### Aviso de Privacidad Simplificado
*(Para imprimirse en la Carta de Consentimiento, conforme al artículo 16, fracción II de la LFPDPPP vigente.)*

**[Nombre completo del médico]**, con domicilio en **[domicilio del consultorio o clínica]**, es responsable del tratamiento de sus datos personales. Se recaban datos de identificación (nombre, CURP, fecha de nacimiento, edad, sexo, número de expediente) y **datos personales sensibles** de salud, así como la grabación de voz de la consulta. Sus datos se utilizan para transcribir la consulta, generar su nota clínica conforme a la NOM-004-SSA3-2012, almacenarla de forma segura y enviarla a su médico; todas estas finalidades son necesarias para su atención y requieren su consentimiento expreso. Puede negarse a la grabación de cualquier consulta, y limitar el uso de sus datos o ejercer sus derechos ARCO ante su médico o al correo admin@clinianotes.com. Consulte el Aviso de Privacidad integral en **clinianotes.com/legal**.
