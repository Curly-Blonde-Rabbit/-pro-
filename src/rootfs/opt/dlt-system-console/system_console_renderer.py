"""Native admin module renderer. Rendering/navigation never invoke system commands."""
from rounded_panel_renderer import RoundedPanel,BUTTONS,hit_test
import pathlib

class SystemConsole(RoundedPanel):
    def __init__(self,asset_path=None):
        super().__init__(asset_path or pathlib.Path(__file__).with_name('system-console-assets.json'))
    def target_at(self,x,y,page='home'):
        if 894<=x<1040 and 42<=y<103:return 'recovery' if page=='home' else 'home'
        n=hit_test(x,y)
        if n is None:return None
        return self.manifest['home_modules' if page=='home' else 'recovery_modules'][n]
    def draw_page(self,mem,pitch,page='home',snapshot=None,selected=None):
        if page not in ('home','recovery'):raise ValueError('unsupported page')
        snapshot=snapshot or {}
        self.blit(mem,pitch,'base-'+page,0,0)
        keys=self.manifest['home_modules' if page=='home' else 'recovery_modules']
        for key,(x,y,w,h) in zip(keys,BUTTONS):
            self.blit(mem,pitch,'module-'+key+('-selected' if key==selected else ''),x,y)
            if page=='home':
                # Never invent online/healthy values when collection has not run.
                value=snapshot.get(key,{}).get('label','—')
                asset='status-'+key+'-'+value
                if asset not in self.assets:asset='status-'+key+'-状态未知'
                self.blit(mem,pitch,asset,x+28,y+151)
