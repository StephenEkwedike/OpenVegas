"""Package the local VS Code experiment without installing dependencies or publishing."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / 'tools/vscode-game-table'
    manifest = '''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="game-table" Version="0.1.0" Publisher="openvegas"/>
<DisplayName>OpenVegas Game Table (Local Preview)</DisplayName><Description xml:space="preserve">Human-controlled OpenVegas game tab.</Description>
<Tags>openvegas</Tags><Categories>Other</Categories><GalleryFlags/>
<Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.85.0"/>
<Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
<Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
<Property Id="Microsoft.VisualStudio.Code.ExecutesCode" Value="true"/></Properties></Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
<Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>'''
    types = '''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/>
<Default Extension="css" ContentType="text/css"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>'''
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('extension.vsixmanifest',manifest)
        z.writestr('[Content_Types].xml',types)
        for path in sorted(source.rglob('*')):
            if path.is_file():
                z.write(path,'extension/'+path.relative_to(source).as_posix())
        for name in ('game-art.js','game-graphics.js'):
            z.write(ROOT/'openvegas/integrations/assets'/name,'extension/media/'+name)
    return output


if __name__ == '__main__':
    print(build(ROOT/'.local/artifacts/openvegas-game-table-0.1.0.vsix'))
